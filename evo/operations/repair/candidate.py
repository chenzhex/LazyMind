from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evo.operations.abtest.materializers import (
    candidate_rag_answer,
    candidate_service,
    compare_eval_detail_for_repair,
)
from evo.operations.analysis.summary import build_analysis_from_answers
from evo.operations.eval.judge import judge_case
from evo.operations.eval.materializers import build_eval_detail_summary

from .errors import EXTERNAL_CHAT_FAILURE_TYPES

PUBLIC_SERVICE_KEYS = {
    'status',
    'service_kind',
    'algorithm_id',
}


def validate_candidate_patch(
    root: Path,
    diff: str,
    plan: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
    baseline_judges: Mapping[str, Mapping[str, Any]],
    eval_policy: Mapping[str, Any],
    candidate_config: Mapping[str, Any],
    ctx: Any,
    trace: Any | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    objective = plan.get('objective') if isinstance(plan.get('objective'), Mapping) else {}
    required = [_text(item) for item in objective.get('validation_case_ids') or [] if _text(item)]
    selected = {case_id: cases[case_id] for case_id in required if case_id in cases}
    missing_cases = [case_id for case_id in required if case_id not in cases]
    missing_baseline = [case_id for case_id in required if case_id in cases and case_id not in baseline_judges]
    if missing_cases or missing_baseline:
        return {
            'status': 'rejected',
            'accepted': False,
            'reason': 'validation_case_coverage_missing',
            'missing_cases': missing_cases,
            'missing_baseline_judges': missing_baseline,
            'case_ids': list(selected),
        }
    if not selected:
        return {'status': 'rejected', 'accepted': False, 'reason': 'no_validation_cases'}
    patch = {'status': 'verified', 'workspace_ref': str(root), 'diff': diff}
    _emit(trace, 'candidate.service_started', status='started', attempt=attempt,
          payload={'case_count': len(selected)})
    try:
        service = candidate_service(candidate_config, patch, ctx)
    except Exception as exc:
        _emit(trace, 'candidate.service_failed', status='failed', attempt=attempt,
              payload={'error_type': type(exc).__name__})
        raise
    public_service = {key: value for key, value in service.items()
                      if key in PUBLIC_SERVICE_KEYS and key != 'healthcheck'}
    health = service.get('healthcheck') if isinstance(service.get('healthcheck'), Mapping) else {}
    public_service['healthcheck'] = {
        key: health.get(key)
        for key in ('status', 'type', 'algorithm_status', 'healthy_instances')
        if key in health
    }
    if service.get('status') != 'ready':
        _emit(trace, 'candidate.service_failed', status='failed', attempt=attempt,
              payload={'reason': 'candidate_service_failed', 'service': public_service})
        return {'status': 'candidate_service_failed', 'accepted': False, 'reason': 'candidate_service_failed',
                'service': public_service, 'case_ids': list(selected)}
    _emit(trace, 'candidate.service_ready', status='completed', attempt=attempt, payload={'service': public_service})
    answers, judges, early_stop_reason = {}, {}, ''
    for case_id, case in selected.items():
        _emit(trace, 'candidate.case_started', status='started', attempt=attempt, payload={'case_id': case_id})
        try:
            answer = candidate_rag_answer(case, service)
            answers[case_id] = answer
            judges[case_id] = judge_case(case, answer, eval_policy)
        except Exception as exc:
            _emit(trace, 'candidate.case_completed', status='failed', attempt=attempt,
                  payload={'case_id': case_id, 'error_type': type(exc).__name__})
            raise
        _emit(trace, 'candidate.case_completed', status='completed', attempt=attempt, payload={
            'case_id': case_id,
            'answer_status': answer.get('status'),
            'trace_id': answer.get('trace_id'),
            'quality_label': judges[case_id].get('quality_label'),
            'answer_correctness': judges[case_id].get('answer_correctness'),
            'overall_score': judges[case_id].get('overall_score'),
        })
        chat_error = answer.get('chat_error') if isinstance(answer.get('chat_error'), Mapping) else {}
        if judges[case_id].get('failure_type') == 'infra_failure' and chat_error.get('type') in EXTERNAL_CHAT_FAILURE_TYPES:
            early_stop_reason = str(chat_error.get('type') or 'candidate_external_failure')
            break
    answer_refs = {
        case_id: {'status': answer.get('status'), 'trace_id': answer.get('trace_id')}
        for case_id, answer in answers.items()
    }
    judge_refs = {
        case_id: {
            'quality_label': judge.get('quality_label'),
            'failure_type': judge.get('failure_type'),
            'retrieval_failure_type': judge.get('retrieval_failure_type'),
            'overall_score': judge.get('overall_score'),
            'answer_correctness': judge.get('answer_correctness'),
        }
        for case_id, judge in judges.items()
    }
    baseline_summary = build_eval_detail_summary(tuple(
        baseline_judges[case_id] for case_id in judges if case_id in baseline_judges
    ))
    candidate_summary = build_eval_detail_summary(tuple(judges[case_id] for case_id in judges))
    candidate_summary = candidate_summary | {'id': 'repair.candidate_eval_summary'}
    comparison = compare_eval_detail_for_repair(baseline_summary, candidate_summary)
    _emit(trace, 'candidate.eval_summary_completed', status='completed', attempt=attempt, payload={
        'case_count': len(selected),
        'evaluated_case_count': len(judges),
        'early_stop_reason': early_stop_reason,
        'execution_failure_count': len(candidate_summary.get('execution_failures') or []),
        'metrics': candidate_summary.get('metrics') if isinstance(candidate_summary.get('metrics'), Mapping) else {},
        'comparison_status': comparison.get('status'),
        'comparison_verdict': comparison.get('verdict'),
    })
    evidence = {
        'case_ids': list(selected),
        'service': public_service,
        'candidate_answer_refs': answer_refs,
        'candidate_judge_refs': judge_refs,
        'candidate_eval_summary': candidate_summary,
        'comparison': comparison,
    }
    if early_stop_reason:
        return {
            'status': 'rejected',
            'accepted': False,
            'reason': f'candidate_eval_stopped:{early_stop_reason}',
            **evidence,
            'evaluated_case_ids': list(judges),
            'early_stop_reason': early_stop_reason,
        }
    try:
        _emit(trace, 'analysis.candidate_started', status='started', attempt=attempt,
              payload={'case_count': len(selected)})
        analysis = build_analysis_from_answers(selected, answers, judges) | {'id': 'repair.candidate_analysis'}
    except Exception as exc:
        _emit(trace, 'analysis.candidate_completed', status='failed', attempt=attempt,
              payload={'error_type': type(exc).__name__})
        return {
            'status': 'rejected',
            'accepted': False,
            'reason': f'candidate_analysis_failed:{type(exc).__name__}',
            **evidence,
            'candidate_analysis_error': str(exc),
        }
    delta = _analysis_delta_from(plan, comparison, analysis, candidate_summary)
    _emit(trace, 'analysis.candidate_completed', status='completed', attempt=attempt,
          payload={'row_count': len(analysis.get('rows') or [])})
    _emit(trace, 'analysis.delta_completed', status='completed', attempt=attempt, payload={
        key: delta.get(key)
        for key in ('target_group_status', 'target_remaining_badcase_count',
                    'target_remaining_delta', 'target_badcase_count', 'new_group_count',
                    'goodcase_guard_status', 'recommended_action')
    })
    accepted, reason = _candidate_gate(comparison, candidate_summary, delta)
    return {
        'status': 'accepted' if accepted else 'rejected',
        'accepted': accepted,
        'reason': reason,
        **evidence,
        'candidate_analysis': analysis,
        'analysis_delta': delta,
    }


def _analysis_delta_from(
    plan: Mapping[str, Any],
    comparison: Mapping[str, Any],
    analysis: Mapping[str, Any],
    candidate_summary: Mapping[str, Any],
) -> dict[str, Any]:
    selected = plan.get('selected_group') if isinstance(plan.get('selected_group'), Mapping) else {}
    target = set((plan.get('objective') or {}).get('target_cases') or [])
    baseline_target_count = len(set(selected.get('case_ids') or []) & target) or len(target)
    rows = [row for row in analysis.get('rows') or [] if isinstance(row, Mapping) and row.get('case_id') in target]
    target_badcases = [row for row in rows if _text(row.get('issue_type')) != 'correct']
    remaining = [
        row for row in rows
        if _text(row.get('affected_block')) == _text(selected.get('function_block_id'))
        and _text(row.get('failure_mode')) == _text(selected.get('failure_mode'))
        and _text(row.get('issue_type')) != 'correct'
    ]
    old_groups = {
        (
            _text(group.get('function_block_id')),
            _text(group.get('issue_type')),
            _text(group.get('failure_mode')),
            _text(group.get('trace_signature')),
        )
        for group in plan.get('repair_group_queue') or []
        if isinstance(group, Mapping)
    }
    new_groups = [
        group for group in analysis.get('repair_group_queue') or []
        if isinstance(group, Mapping)
        and (
            _text(group.get('function_block_id')),
            _text(group.get('issue_type')),
            _text(group.get('failure_mode')),
            _text(group.get('trace_signature')),
        ) not in old_groups
    ]
    metrics = comparison.get('metrics') if isinstance(comparison.get('metrics'), Mapping) else {}
    delta = metrics.get('delta') if isinstance(metrics.get('delta'), Mapping) else {}
    target_remaining_delta = baseline_target_count - len(remaining)
    resolved = not remaining and not target_badcases
    improved = target_remaining_delta > 0
    status = 'resolved' if resolved else 'improved' if improved else 'unchanged'
    goodcase_guard = comparison.get('goodcase_guard') if isinstance(comparison.get('goodcase_guard'), Mapping) else {}
    if goodcase_guard.get('status') == 'failed':
        status = 'regressed'
    recommended_action = (
        'accept_patch'
        if resolved and not new_groups else 'continue_current_patch'
        if improved and goodcase_guard.get('status') != 'failed' else 'rollback_to_baseline'
    )
    return {
        'status': 'completed',
        'target_group_status': status,
        'target_remaining_badcase_count': len(remaining),
        'target_remaining_delta': target_remaining_delta,
        'target_badcase_count': len(target_badcases),
        'target_total': len(target),
        'new_group_count': len(new_groups),
        'new_groups': new_groups[:5],
        'goodcase_guard_status': goodcase_guard.get('status') or '',
        'metric_delta': delta,
        'execution_failures': candidate_summary.get('execution_failures') or [],
        'recommended_action': recommended_action,
    }


def _candidate_gate(
    comparison: Mapping[str, Any],
    candidate_summary: Mapping[str, Any],
    delta: Mapping[str, Any],
) -> tuple[bool, str]:
    metrics = delta.get('metric_delta') if isinstance(delta.get('metric_delta'), Mapping) else {}
    if comparison.get('status') != 'completed':
        return False, _text(comparison.get('verdict')) or 'comparison_not_completed'
    if candidate_summary.get('execution_failures'):
        return False, 'candidate_execution_failed'
    if comparison.get('goodcase_guard', {}).get('status') == 'failed':
        return False, 'goodcase_guard_failed'
    if delta.get('new_group_count'):
        return False, 'target_followup_groups_detected'
    if delta.get('target_group_status') not in {'resolved', 'improved'}:
        return False, 'target_group_not_improved'
    if float(metrics.get('answer_correctness') or metrics.get('overall_score') or 0.0) < 0.0001:
        return False, 'metric_not_improved'
    return True, 'target_group_improved'


def _emit(trace: Any | None, event_type: str, **kwargs: Any) -> None:
    if trace is None:
        return
    try:
        trace.emit(event_type, **kwargs)
    except Exception:
        pass


def _text(value: Any) -> str:
    return str(value or '').strip()
