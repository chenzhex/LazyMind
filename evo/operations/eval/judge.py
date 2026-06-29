from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Literal

from json_repair import repair_json
from pydantic import BaseModel, ConfigDict, Field, ValidationError

QualityLabel = Literal['good', 'partial', 'bad', 'infra_failure']
AnswerFailureType = Literal[
    'none',
    'wrong_answer',
    'partial_answer',
    'question_not_answered',
    'format_error',
    'hallucination',
]
FailureType = Literal[
    'none',
    'wrong_answer',
    'partial_answer',
    'question_not_answered',
    'format_error',
    'hallucination',
    'infra_failure',
    'judge_contract_error',
    'dataset_contract_error',
]
RetrievalFailureType = Literal['none', 'retrieval_miss', 'retrieval_partial', 'retrieval_noise', 'not_applicable']
SCORES = ('answer_correctness', 'answer_relevance', 'completeness', 'groundedness', 'format_compliance')


class JudgePayload(BaseModel):
    model_config = ConfigDict(extra='ignore')

    answer_correctness: float = Field(ge=0.0, le=1.0)
    answer_relevance: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    groundedness: float = Field(ge=0.0, le=1.0)
    format_compliance: float = Field(ge=0.0, le=1.0)
    failure_type: AnswerFailureType
    reason: str
    defect: str


def judge_answer(case: Mapping[str, Any], rag_answer: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    if rag_answer.get('status') != 'ok':
        failure_type, reason = _runtime_failure(rag_answer)
        return _failure_result(case, rag_answer, policy, failure_type, reason)
    try:
        payload = _judge_payload(case, rag_answer, policy)
        return _result(case, rag_answer, policy, payload)
    except Exception as exc:
        return judge_contract_error(case, rag_answer, policy, str(exc))


def judge_contract_error(
    case: Mapping[str, Any],
    rag_answer: Mapping[str, Any],
    policy: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return _failure_result(case, rag_answer, policy, 'judge_contract_error', reason)


def _judge_payload(case: Mapping[str, Any], rag_answer: Mapping[str, Any], policy: Mapping[str, Any]) -> JudgePayload:
    if isinstance(policy.get('judge_result'), Mapping):
        return JudgePayload.model_validate(policy['judge_result'])
    from evo.llm import LazyLLMClient

    llm_config = policy.get('judge_llm_config') if isinstance(policy.get('judge_llm_config'), Mapping) else {}
    if not isinstance(llm_config.get('evo_llm'), Mapping):
        raise ValueError('eval.policy.judge_llm_config.evo_llm missing; eval must use core model-config injection')
    client = LazyLLMClient(
        llm_config=llm_config,
        model='evo_llm',
    )
    raw = str(client(_judge_prompt(case, rag_answer, policy), stream=False))
    repaired = repair_json(raw, return_objects=True)
    if not isinstance(repaired, Mapping):
        raise ValueError('judge did not return a JSON object')
    return JudgePayload.model_validate(repaired)


def _result(
    case: Mapping[str, Any],
    rag_answer: Mapping[str, Any],
    policy: Mapping[str, Any],
    payload: JudgePayload,
) -> dict[str, Any]:
    retrieval = _retrieval(case, rag_answer)
    answer_quality = _round(sum((
        0.45 * payload.answer_correctness,
        0.25 * payload.completeness,
        0.20 * payload.groundedness,
        0.10 * payload.answer_relevance,
    )))
    retrieval_quality = retrieval['retrieval_quality_score']
    has_refs = retrieval['retrieval_failure_type'] != 'not_applicable'
    overall = _round(0.80 * answer_quality + 0.20 * retrieval_quality) if has_refs else answer_quality
    label, failure_type = _label(payload, policy, overall)
    if retrieval['retrieval_failure_type'] == 'retrieval_miss' and label == 'good':
        label = 'partial'
    if retrieval['retrieval_failure_type'] != 'none' and failure_type == 'none':
        failure_type = 'partial_answer'
    return _base(case, rag_answer, policy) | payload.model_dump() | retrieval | {
        'answer_quality_score': answer_quality,
        'retrieval_quality_score': retrieval_quality,
        'overall_score': overall,
        'quality_label': label,
        'failure_type': failure_type,
        'is_correct': label == 'good',
    }


def _failure_result(
    case: Mapping[str, Any],
    rag_answer: Mapping[str, Any],
    policy: Mapping[str, Any],
    failure_type: FailureType,
    reason: str,
) -> dict[str, Any]:
    return _base(case, rag_answer, policy) | {
        **{key: 0.0 for key in SCORES},
        'answer_quality_score': 0.0,
        **{key: 0.0 for key in ('chunk_recall', 'chunk_precision', 'doc_recall', 'doc_precision',
                                'context_recall', 'context_precision')},
        'retrieval_quality_score': 0.0,
        'overall_score': 0.0,
        'retrieval_failure_type': 'not_applicable',
        'quality_label': 'infra_failure',
        'failure_type': failure_type,
        'is_correct': False,
        'reason': reason,
        'defect': _runtime_detail(rag_answer) or failure_type,
    }


def _base(case: Mapping[str, Any], rag_answer: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'case_id': str(case.get('id') or rag_answer.get('case_id') or ''),
        'case': dict(case),
        'rag_answer': dict(rag_answer),
        'trace_id': str(rag_answer.get('trace_id') or ''),
        'target': dict(rag_answer.get('target') or {}),
        'eval_policy': _policy_view(policy),
        'tool_errors': list(rag_answer.get('tool_errors') or []),
    }


def _retrieval(case: Mapping[str, Any], rag_answer: Mapping[str, Any]) -> dict[str, Any]:
    ref_chunks, got_chunks = _ids(case.get('reference_chunk_ids')), _ids(rag_answer.get('chunk_ids'))
    ref_docs, got_docs = _ids(case.get('reference_doc_ids')), _ids(rag_answer.get('doc_ids'))
    chunk_recall, chunk_precision = _overlap(ref_chunks, got_chunks)
    doc_recall, doc_precision = _overlap(ref_docs, got_docs)
    recall = chunk_recall if ref_chunks else doc_recall
    precision = chunk_precision if ref_chunks else doc_precision
    if not ref_chunks and not ref_docs:
        failure = 'not_applicable'
        recall = precision = 1.0
    elif recall == 0.0:
        failure = 'retrieval_miss'
    elif recall < 1.0:
        failure = 'retrieval_partial'
    elif precision < 0.5:
        failure = 'retrieval_noise'
    else:
        failure = 'none'
    return {
        'chunk_recall': chunk_recall,
        'chunk_precision': chunk_precision,
        'doc_recall': doc_recall,
        'doc_precision': doc_precision,
        'context_recall': recall,
        'context_precision': precision,
        'retrieval_quality_score': _round(0.70 * recall + 0.30 * precision),
        'retrieval_failure_type': failure,
    }


def _label(payload: JudgePayload, policy: Mapping[str, Any], overall: float) -> tuple[QualityLabel, FailureType]:
    failure = payload.failure_type if payload.failure_type != 'none' else _metric_failure(payload)
    format_ok = payload.format_compliance >= 1.0
    if not format_ok:
        failure = 'format_error'
    gates_ok = (
        payload.answer_correctness >= float(policy.get('answer_correctness_floor') or 0.6)
        and payload.groundedness >= float(policy.get('groundedness_floor') or 0.6)
        and payload.answer_relevance >= float(policy.get('answer_relevance_floor') or 0.6)
        and format_ok
        and failure == 'none'
    )
    if gates_ok and overall >= float(policy.get('answer_good_threshold') or 0.8):
        return 'good', 'none'
    if overall >= float(policy.get('answer_partial_threshold') or 0.5):
        return 'partial', failure if failure != 'none' else 'partial_answer'
    return 'bad', failure if failure != 'none' else 'wrong_answer'


def _metric_failure(payload: JudgePayload) -> FailureType:
    if payload.answer_relevance < 0.4:
        return 'question_not_answered'
    if payload.groundedness < 0.4:
        return 'hallucination'
    if payload.answer_correctness < 0.5:
        return 'wrong_answer'
    if payload.completeness < 0.6:
        return 'partial_answer'
    return 'none'


def _judge_prompt(case: Mapping[str, Any], rag_answer: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    rubric = str(policy.get('rubric') or 'Use the reference answer, reference context, and grading guidance.')
    payload = {
        'question': case.get('question'),
        'reference_answer': case.get('answer'),
        'grading_guidance': case.get('grading_guidance'),
        'reference_context': case.get('reference_context'),
        'rag_answer': rag_answer.get('answer'),
        'retrieved_contexts': rag_answer.get('contexts'),
    }
    return (
        'Judge one RAG answer. Return one JSON object only, no markdown. '
        f'Scores must be floats from 0 to 1. Required score keys: {", ".join(SCORES)}. '
        'Also return failure_type, reason, defect. '
        'failure_type must be one of none, wrong_answer, partial_answer, question_not_answered, '
        'format_error, hallucination. Do not emit infra_failure, judge_contract_error, or '
        'dataset_contract_error; those are runtime failures handled outside the LLM judge.\n'
        f'rubric: {rubric}\n'
        f'case_json: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}'
    )


def _overlap(reference: set[str], retrieved: set[str]) -> tuple[float, float]:
    if not reference:
        return 0.0, 0.0
    hit = len(reference & retrieved)
    return _round(hit / len(reference)), _round(hit / len(retrieved)) if retrieved else 0.0


def _ids(value: Any) -> set[str]:
    if isinstance(value, str):
        items = [value] if value.strip() else []
    else:
        items = list(value or [])
    return {str(item).strip() for item in items if str(item or '').strip()}


def _round(value: float) -> float:
    if not math.isfinite(float(value)):
        raise ValueError('score must be finite')
    return round(max(0.0, min(1.0, float(value))), 4)


def _policy_view(policy: Mapping[str, Any]) -> dict[str, Any]:
    keys = ('answer_good_threshold', 'answer_partial_threshold', 'answer_correctness_floor',
            'groundedness_floor', 'answer_relevance_floor', 'judge_schema_version', 'rubric', 'judge_model')
    return {key: policy[key] for key in keys if key in policy}


def _runtime_failure(rag_answer: Mapping[str, Any]) -> tuple[FailureType, str]:
    error = rag_answer.get('chat_error')
    detail = str(error.get('type') or 'chat_failed') if isinstance(error, Mapping) else 'chat_failed'
    message = str(error.get('message') or detail) if isinstance(error, Mapping) else detail
    if detail == 'dataset_contract_error':
        return 'dataset_contract_error', message
    return 'infra_failure', f'{detail}: {message}' if message != detail else detail


def _runtime_detail(rag_answer: Mapping[str, Any]) -> str:
    error = rag_answer.get('chat_error')
    return str(error.get('type') or '') if isinstance(error, Mapping) else ''


def validate_judge_result(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        _ResultModel.model_validate(value)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return dict(value)


class _ResultModel(JudgePayload):
    model_config = ConfigDict(extra='allow')

    case_id: str
    answer_quality_score: float = Field(ge=0.0, le=1.0)
    retrieval_quality_score: float = Field(ge=0.0, le=1.0)
    overall_score: float = Field(ge=0.0, le=1.0)
    retrieval_failure_type: RetrievalFailureType
    quality_label: QualityLabel
    failure_type: FailureType
    is_correct: bool
    reason: str
