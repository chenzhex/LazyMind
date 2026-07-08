from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import EXTERNAL_CHAT_FAILURE_TYPES

TARGET_STATUS_RANK = {'resolved': 3, 'improved': 2, 'unchanged': 1, 'regressed': 0}
EXTERNAL_CANDIDATE_STOP_REASONS = frozenset(
    f'candidate_eval_stopped:{error}' for error in EXTERNAL_CHAT_FAILURE_TYPES
)


def patch_base_decision(
    pre: Mapping[str, Any],
    candidate: Mapping[str, Any],
    delta: Mapping[str, Any],
    best: Mapping[str, Any],
) -> dict[str, Any]:
    if pre.get('status') != 'passed':
        if reason := _opencode_block_reason(pre):
            return {'action': 'blocked', 'reason': reason}
        return {'action': 'rollback_to_baseline', 'reason': _text(pre.get('reason')) or 'pre_validation_failed'}
    if candidate.get('accepted'):
        return {'action': 'accept_patch', 'reason': candidate.get('reason') or 'accepted'}
    if candidate.get('status') == 'candidate_service_failed':
        service = candidate.get('service') if isinstance(candidate.get('service'), Mapping) else {}
        health = service.get('healthcheck') if isinstance(service.get('healthcheck'), Mapping) else {}
        htype = _text(health.get('type'))
        retryable = {'router_timeout'}
        if htype in retryable:
            if _best_can_fork(best):
                return {'action': 'fork_from_best_attempt', 'reason': 'previous attempt has best target topology'}
            return {'action': 'rollback_to_baseline', 'reason': f'candidate_service_retryable:{htype}'}
        external = {
            'ConnectError',
            'ConnectTimeout',
            'HTTPStatusError',
            'PoolTimeout',
            'ReadTimeout',
            'RemoteProtocolError',
            'TimeoutError',
            'router_config_error',
            'router_http_error',
            'router_protocol_error',
            'router_timeout',
            'router_transport_error',
        }
        if htype in external:
            return {'action': 'blocked', 'reason': f'external candidate service unavailable: {htype}'}
        return {'action': 'rollback_to_baseline', 'reason': 'candidate_service_failed'}
    failures = candidate.get('candidate_eval_summary', {}).get('execution_failures', [])
    if any(_router_chat_failure(row) for row in failures):
        return {'action': 'blocked', 'reason': 'external candidate routing unavailable'}
    if _text(candidate.get('reason')).startswith('candidate_analysis_failed:'):
        return {'action': 'blocked', 'reason': candidate.get('reason')}
    comparison = candidate.get('comparison') if isinstance(candidate.get('comparison'), Mapping) else {}
    summary = (
        candidate.get('candidate_eval_summary')
        if isinstance(candidate.get('candidate_eval_summary'), Mapping) else {}
    )
    if summary.get('execution_failures'):
        return {'action': 'rollback_to_baseline', 'reason': 'candidate_execution_failed'}
    if comparison and comparison.get('status') != 'completed':
        return {'action': 'rollback_to_baseline', 'reason': 'candidate_eval_not_completed'}
    if _should_continue_current_patch(delta):
        reason = (
            'target topology improved with follow-up groups'
            if delta.get('new_group_count') else 'target topology improved below acceptance threshold'
        )
        return {'action': 'continue_current_patch', 'reason': reason}
    if _best_can_fork(best):
        return {'action': 'fork_from_best_attempt', 'reason': 'previous attempt has best target topology'}
    return {'action': 'rollback_to_baseline', 'reason': candidate.get('reason') or 'candidate_rejected'}


def select_best_attempt(best: Mapping[str, Any], attempt: Mapping[str, Any]) -> Mapping[str, Any]:
    if not _eligible_best_attempt(attempt):
        return best
    if not best or _attempt_score(attempt) > _attempt_score(best):
        return dict(attempt)
    return best


def _router_chat_failure(row: Mapping[str, Any]) -> bool:
    reason = _text(row.get('reason'))
    chat_error_type = _text(row.get('chat_error_type'))
    return (
        chat_error_type in EXTERNAL_CHAT_FAILURE_TYPES
        or reason in EXTERNAL_CANDIDATE_STOP_REASONS
    )


def _opencode_block_reason(pre: Mapping[str, Any]) -> str:
    if not _text(pre.get('reason')).startswith('opencode_failed:'):
        return ''
    error = pre.get('opencode_error') if isinstance(pre.get('opencode_error'), Mapping) else {}
    error_type = _text(error.get('type'))
    if error_type in {'configuration_error', 'process_start_failed', 'prompt_write_failed'}:
        return f'external opencode unavailable: {error_type}'
    detail = error.get('error') if isinstance(error.get('error'), Mapping) else error
    data = detail.get('data') if isinstance(detail.get('data'), Mapping) else {}
    status_code = _int(data.get('statusCode') or data.get('status_code'))
    retryable = data.get('isRetryable')
    name = _text(detail.get('name'))
    api_error = name in {'APIError', 'AuthenticationError', 'PermissionDeniedError', 'RateLimitError', 'NotFoundError'}
    hard_http_failure = status_code in {400, 401, 403, 404, 429}
    if hard_http_failure or (api_error and retryable is False):
        label = f'HTTP {status_code}' if status_code else name
        return f'external opencode api unavailable: {label}'
    return ''


def _best_can_fork(attempt: Mapping[str, Any]) -> bool:
    delta = attempt.get('analysis_delta') if isinstance(attempt.get('analysis_delta'), Mapping) else {}
    pre = attempt.get('pre_validation') if isinstance(attempt.get('pre_validation'), Mapping) else {}
    candidate = attempt.get('candidate_validation') if isinstance(attempt.get('candidate_validation'), Mapping) else {}
    summary = candidate.get('candidate_eval_summary') if isinstance(candidate.get('candidate_eval_summary'), Mapping) else {}
    return (
        pre.get('status') == 'passed'
        and not summary.get('execution_failures')
        and candidate.get('reason') == 'metric_not_improved'
        and delta.get('target_group_status') == 'improved'
        and not delta.get('new_group_count')
        and _positive_metric(delta)
        and _has_target_progress(delta)
    )


def _eligible_best_attempt(attempt: Mapping[str, Any]) -> bool:
    delta = attempt.get('analysis_delta') if isinstance(attempt.get('analysis_delta'), Mapping) else {}
    pre = attempt.get('pre_validation') if isinstance(attempt.get('pre_validation'), Mapping) else {}
    return (
        pre.get('status') == 'passed'
        and delta.get('status') == 'completed'
        and not delta.get('new_group_count')
        and (
            delta.get('target_group_status') == 'resolved'
            or (delta.get('target_group_status') == 'improved' and _positive_metric(delta))
        )
    )


def _attempt_score(attempt: Mapping[str, Any]) -> tuple[int, float]:
    delta = attempt.get('analysis_delta') if isinstance(attempt.get('analysis_delta'), Mapping) else {}
    return TARGET_STATUS_RANK.get(delta.get('target_group_status'), 0), _metric_value(delta)


def _has_target_progress(delta: Mapping[str, Any]) -> bool:
    if delta.get('status') != 'completed':
        return False
    if delta.get('target_group_status') not in {'resolved', 'improved'}:
        return False
    if delta.get('goodcase_guard_status') != 'passed':
        return False
    return int(delta.get('target_remaining_delta') or 0) > 0 or _positive_metric(delta)


def _should_continue_current_patch(delta: Mapping[str, Any]) -> bool:
    action = _text(delta.get('recommended_action'))
    if action and action != 'continue_current_patch':
        return False
    return _has_target_progress(delta)


def _positive_metric(delta: Mapping[str, Any]) -> bool:
    return _metric_value(delta) > 0.0


def _metric_value(delta: Mapping[str, Any]) -> float:
    metrics = delta.get('target_metric_delta') if isinstance(delta.get('target_metric_delta'), Mapping) else {}
    primary = [_text(item) for item in delta.get('primary_metrics') or () if _text(item)]
    values = [_float(metrics.get(metric)) for metric in primary if metric in metrics]
    if values:
        return max(values)
    aggregate = delta.get('metric_delta') if isinstance(delta.get('metric_delta'), Mapping) else {}
    return max(_float(aggregate.get('answer_correctness')), _float(aggregate.get('overall_score')))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return str(value or '').strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
