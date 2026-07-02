from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any, Callable

from evo.operations.public_contracts import build_eval_summary_root

from .answer import call_chat_answer, failed_rag_answer
from .judge import judge_answer, judge_contract_error, validate_judge_result

UNSCORED = {'infra_failure', 'judge_contract_error', 'dataset_contract_error'}
SCORES = (
    'answer_correctness',
    'answer_relevance',
    'completeness',
    'groundedness',
    'format_compliance',
    'answer_quality_score',
    'retrieval_quality_score',
    'overall_score',
)


def eval_materializers() -> dict[str, Callable[[Any, Mapping[str, object]], Mapping[str, object]]]:
    def answer(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        case = _mapping(inputs['case'], 'case')
        target_config = _mapping(inputs.get('target_config') or {}, 'target_config')
        case_id = str(case.get('id') or '')
        by_case = target_config.get('case_metadata_by_id')
        metadata = case.get('case_metadata') if isinstance(case.get('case_metadata'), Mapping) else {}
        preparation = case.get('source_preparation') if isinstance(case.get('source_preparation'), Mapping) else {}
        case_source = preparation.get('case_source') if isinstance(preparation.get('case_source'), Mapping) else {}
        kb_id = ''
        if isinstance(by_case, Mapping) and isinstance(by_case.get(case_id), Mapping):
            kb_id = str(by_case[case_id].get('kb_id') or '').strip()
        kb_id = kb_id or str(metadata.get('kb_id') or case_source.get('kb_id') or '').strip()
        target = {'target_chat_url': str(target_config.get('target_chat_url') or ''), 'kb_id': kb_id}
        if not kb_id:
            value = failed_rag_answer(case, {}, target, 'dataset_contract_error', 'case routing metadata missing kb_id')
        elif not _has_role(target_config.get('llm_config'), 'llm'):
            value = failed_rag_answer(case, {}, target, 'chat_config_error',
                                      'eval.target_config.llm_config.llm missing; '
                                      'eval must be launched through core model-config injection')
        else:
            value = call_chat_answer(case, target_config, kb_id)
        return {'answer': value}

    def judge(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        case = _mapping(inputs['case'], 'case')
        rag_answer = _mapping(inputs['answer'], 'answer')
        policy = _mapping(inputs.get('policy') or {}, 'policy')
        try:
            value = validate_judge_result(judge_answer(case, rag_answer, policy))
        except Exception as exc:
            value = validate_judge_result(judge_contract_error(case, rag_answer, policy, str(exc)))
        return {'judge': value}

    def summary(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        judges = inputs.get('judges')
        if not isinstance(judges, tuple):
            raise ValueError('eval.summary judges input must be a partitioned tuple')
        return {'summary': build_eval_summary_root(ctx.run_id, judges)}

    return {'eval.answer': answer, 'eval.judge': judge, 'eval.summary': summary}


def build_eval_detail_summary(judges: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    for index, judge in enumerate(judges, 1):
        if not isinstance(judge, Mapping):
            rows.append({
                'case_id': f'invalid_{index:04d}',
                'kb_id': '',
                'question': '',
                'question_type': '',
                'ground_truth': '',
                'rag_answer': '',
                **{key: 0.0 for key in ('answer_score', 'retrieval_score', *SCORES)},
                'quality_label': 'infra_failure',
                'failure_type': 'judge_contract_error',
                'retrieval_failure_type': 'not_applicable',
                'reason': 'judge result is not a mapping',
                'defect': 'judge_contract_error',
                'reference_chunk_ids': [],
                'reference_doc_ids': [],
                'retrieve_chunk_ids': [],
                'retrieve_doc_ids': [],
                'retrieve_contexts': [],
                'retrieved_contexts': [],
                'trace_id': '',
                'target': {},
            })
            continue
        case = judge.get('case') if isinstance(judge.get('case'), Mapping) else {}
        answer = judge.get('rag_answer') if isinstance(judge.get('rag_answer'), Mapping) else {}
        target = judge.get('target') if isinstance(judge.get('target'), Mapping) else {}
        rows.append({
            'case_id': str(judge.get('case_id') or case.get('id') or ''),
            'kb_id': str(target.get('kb_id') or ''),
            'question': str(case.get('question') or ''),
            'question_type': str(case.get('question_type') or ''),
            'ground_truth': case.get('answer'),
            'rag_answer': answer.get('answer'),
            **{key: judge.get(key, 0.0) for key in SCORES},
            'answer_score': judge.get('answer_quality_score', 0.0),
            'retrieval_score': judge.get('retrieval_quality_score', 0.0),
            'quality_label': str(judge.get('quality_label') or ''),
            'failure_type': str(judge.get('failure_type') or ''),
            'retrieval_failure_type': str(judge.get('retrieval_failure_type') or ''),
            'reason': str(judge.get('reason') or ''),
            'defect': str(judge.get('defect') or ''),
            'reference_chunk_ids': case.get('reference_chunk_ids') or [],
            'reference_doc_ids': case.get('reference_doc_ids') or [],
            'retrieve_chunk_ids': answer.get('chunk_ids') or [],
            'retrieve_doc_ids': answer.get('doc_ids') or [],
            'retrieve_contexts': answer.get('contexts') or [],
            'retrieved_contexts': answer.get('contexts') or [],
            'trace_id': str(judge.get('trace_id') or ''),
            'target': dict(target),
        })
    scored = [row for row in rows if row['failure_type'] not in UNSCORED and row['quality_label'] != 'infra_failure']
    failures = [
        {
            'case_id': str(row.get('case_id') or ''),
            'kb_id': str(row.get('kb_id') or ''),
            'failure_type': str(row.get('failure_type') or ''),
            'reason': str(row.get('reason') or ''),
        }
        for row in rows
        if row['failure_type'] in {'infra_failure', 'judge_contract_error', 'dataset_contract_error'}
    ]
    routing_failures = [row for row in failures if row['failure_type'] == 'dataset_contract_error']
    execution_failures = [row for row in failures if row['failure_type'] != 'dataset_contract_error']
    return {
        'id': 'eval.summary',
        'total': len(rows),
        'case_ids': [row['case_id'] for row in rows],
        'metrics': {
            'scored_count': len(scored),
            'overall_score_avg': _avg(scored, 'overall_score'),
            'answer_quality_score_avg': _avg(scored, 'answer_quality_score'),
            'retrieval_quality_score_avg': _avg(
                [row for row in scored if row['retrieval_failure_type'] != 'not_applicable'],
                'retrieval_quality_score',
            ),
            'answer_correctness_avg': _avg(scored, 'answer_correctness'),
            'groundedness_avg': _avg(scored, 'groundedness'),
            'answer_relevance_avg': _avg(scored, 'answer_relevance'),
            'correct_rate': round(sum(1 for row in scored if row['quality_label'] == 'good') / len(scored), 4)
            if scored else 0.0,
        },
        'quality_counts': dict(Counter(row['quality_label'] for row in rows)),
        'failure_type_counts': dict(Counter(row['failure_type'] for row in rows)),
        'retrieval_failure_type_counts': dict(Counter(row['retrieval_failure_type'] for row in rows)),
        'bad_cases': [row for row in rows if row['quality_label'] != 'good'],
        'routing_failures': routing_failures,
        'execution_failures': execution_failures,
        'checks': {'ready': not routing_failures and not execution_failures,
                   'errors': routing_failures + execution_failures},
        'rows': rows,
    }


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value


def _has_role(value: object, role_name: str) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get(role_name), Mapping) and bool(value[role_name])


def _avg(rows: list[Mapping[str, Any]], key: str) -> float:
    values = [float(row.get(key) or 0.0) for row in rows]
    return round(sum(values) / len(values), 4) if values else 0.0
