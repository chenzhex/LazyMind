from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from .answer import call_chat_answer, failed_rag_answer
from .judge import judge_answer, judge_contract_error, validate_judge_result
from .summary import summarize_eval


def eval_materializers() -> dict[str, Callable[[Any, Mapping[str, object]], Mapping[str, object]]]:
    def answer(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        case = _mapping(inputs['case'], 'case')
        target_config = _mapping(inputs.get('target_config') or {}, 'target_config')
        kb_id = _kb_id(case, target_config)
        if not kb_id:
            target = {'target_chat_url': str(target_config.get('target_chat_url') or ''), 'kb_id': ''}
            value = failed_rag_answer(case, {}, target, 'dataset_contract_error', 'case routing metadata missing kb_id')
        elif ';' in kb_id:
            target = {'target_chat_url': str(target_config.get('target_chat_url') or ''), 'kb_id': kb_id}
            value = failed_rag_answer(case, {}, target, 'dataset_contract_error',
                                      'case routing metadata has multiple kb_id values')
        elif not _has_role(target_config.get('llm_config'), 'llm'):
            target = {'target_chat_url': str(target_config.get('target_chat_url') or ''), 'kb_id': kb_id}
            value = failed_rag_answer(case, {}, target, 'chat_config_error',
                                      'eval.target_config.llm_config.llm missing; '
                                      'eval must be launched through core model-config injection')
        else:
            value = call_chat_answer(case, target_config, kb_id)
        return {'answer': value}

    def judge(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        case = _mapping(inputs['case'], 'case')
        policy = _mapping(inputs.get('policy') or {}, 'policy')
        rag_answer = _mapping(inputs['answer'], 'answer')
        try:
            value = validate_judge_result(judge_answer(case, rag_answer, policy))
        except Exception as exc:
            value = validate_judge_result(judge_contract_error(case, rag_answer, policy, str(exc)))
        return {'judge': value}

    def summary(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        judges = inputs.get('judges')
        if not isinstance(judges, tuple):
            raise ValueError('eval.summary judges input must be a partitioned tuple')
        return {'summary': summarize_eval(judges)}

    return {'eval.answer': answer, 'eval.judge': judge, 'eval.summary': summary}


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value


def _kb_id(case: Mapping[str, Any], target_config: Mapping[str, Any]) -> str:
    by_case = target_config.get('case_metadata_by_id')
    case_id = str(case.get('id') or '')
    if isinstance(by_case, Mapping) and isinstance(by_case.get(case_id), Mapping):
        return str(by_case[case_id].get('kb_id') or '').strip()
    metadata = case.get('case_metadata') if isinstance(case.get('case_metadata'), Mapping) else {}
    return str(metadata.get('kb_id') or '').strip()


def _has_role(value: object, role_name: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    role = value.get(role_name)
    return isinstance(role, Mapping) and bool(role)
