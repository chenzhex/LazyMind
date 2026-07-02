from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse, urlunparse

import httpx

from evo.operations.eval.answer import call_chat_answer, failed_rag_answer
from evo.operations.eval.judge import judge_answer, judge_contract_error, validate_judge_result
from evo.operations.public_contracts import build_abtest_comparison_root, build_eval_summary_root

ENV_PASSTHROUGH = (
    'LAZYMIND_DOCUMENT_SERVER_URL',
    'LAZYMIND_DOCUMENT_PROCESSOR_URL',
    'LAZYMIND_SEGMENT_STORE_TYPE',
    'LAZYMIND_SEGMENT_STORE_URI_OR_PATH',
    'LAZYMIND_SHARED_UPLOAD_DIR',
    'LAZYMIND_MOUNT_BASE_DIR',
    'LAZYMIND_AGENTIC_WORKSPACE',
    'LAZYMIND_CORE_API_URL',
    'LAZYMIND_CORE_SERVICE_URL',
    'LAZYMIND_CORE_DATABASE_URL',
    'LAZYMIND_DATABASE_URL',
    'LAZYMIND_MODEL_CONFIG_PATH',
    'LAZYLLM_INIT_DOC',
    'LAZYLLM_TRACE_ENABLED',
    'LAZYLLM_TRACE_BACKEND',
    'LAZYLLM_TRACE_LOCAL_STORAGE_DIR',
    'LAZYLLM_TRACE_CONSUME_BACKEND',
)
COMPARE_METRICS = (
    'overall_score',
    'answer_quality_score',
    'retrieval_quality_score',
    'answer_correctness',
    'groundedness',
    'correct_rate',
)
SAFE_ID = re.compile(r'[^A-Za-z0-9_.-]+')


def abtest_materializers() -> dict[str, Callable[[Any, Mapping[str, object]], Mapping[str, object]]]:
    def service(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        patch = _service_patch(_mapping(inputs['patch'], 'patch'), _mapping(inputs['workspace'], 'workspace'))
        return {'service': candidate_service(_mapping(inputs['config'], 'config'), patch, ctx)}

    def answer(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        return {'answer': candidate_rag_answer(_mapping(inputs['case'], 'case'),
                                               _mapping(inputs['service'], 'service'))}

    def judge(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        case = _mapping(inputs['case'], 'case')
        rag_answer = _mapping(inputs['answer'], 'answer')
        policy = _mapping(inputs.get('policy') or {}, 'policy')
        return {'judge': candidate_judge_result(case, rag_answer, policy)}

    def summary(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        judges = inputs.get('judges')
        if not isinstance(judges, tuple):
            raise ValueError('abtest.candidate_eval_summary judges input must be a partitioned tuple')
        return {'summary': build_eval_summary_root(ctx.run_id, judges)}

    def compare(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        return {'comparison': build_abtest_comparison_root(
            ctx.run_id,
            _mapping(inputs['baseline'], 'baseline'),
            _mapping(inputs['candidate'], 'candidate'),
            _mapping(inputs['service'], 'service'),
        )}

    return {
        'abtest.candidate_service': service,
        'abtest.candidate_rag_answer': answer,
        'abtest.candidate_judge': judge,
        'abtest.candidate_eval_summary': summary,
        'abtest.compare': compare,
    }


def candidate_service(config: Mapping[str, Any], patch: Mapping[str, Any], ctx: Any | None = None) -> dict[str, Any]:
    base = {'candidate_config': dict(config), 'patch_status': _text(patch.get('status'))}
    if patch.get('status') != 'verified':
        return base | {'status': 'skipped', 'healthcheck': {'status': 'skipped'}}
    try:
        algorithm_id = _candidate_algorithm_id(config, patch, getattr(ctx, 'run_id', 'run'))
        target_url = _chat_url(config.get('target_chat_url') or os.getenv('LAZYMIND_EVO_TARGET_CHAT_URL'))
        admin_url = _origin(config.get('router_admin_url') or os.getenv('LAZYMIND_EVO_ROUTER_ADMIN_URL') or target_url)
        code_path = _code_path(config, patch)
        body = {
            'id': algorithm_id,
            'name': _text(config.get('name')) or algorithm_id,
            'code_path': code_path,
            'instance_count': _int_between(config.get('instance_count'), 1, 1, 4),
            'config': _candidate_env(config, algorithm_id),
        }
        timeout_s = _int_between(config.get('startup_timeout_s') or config.get('startup_timeout_seconds'),
                                 180, 10, 900)
        existing = _router_get(admin_url, algorithm_id, timeout_s=10)
        if existing and not _same_registration(existing, body):
            return base | _failed_service(algorithm_id, target_url, admin_url, code_path, 'candidate_id_conflict',
                                          'algorithm_id already exists with different code_path/config')
        registered = {'reused': True} if existing else _router_request(
            'POST',
            f'{admin_url}/inner/algorithm/register',
            body=body,
            timeout_s=timeout_s,
        )
        ready = _wait_ready(admin_url, algorithm_id, timeout_s)
        return base | {
            'status': 'ready',
            'service_kind': 'router_algorithm',
            'algorithm_id': algorithm_id,
            'service_url': target_url,
            'router_admin_url': admin_url,
            'workspace_ref': _text(patch.get('workspace_ref')),
            'code_path': code_path,
            'register_request': body,
            'register_response': registered,
            'healthcheck': _healthcheck(ready),
        }
    except Exception as exc:
        return base | _failed_service('', '', '', '', type(exc).__name__, str(exc))


def _service_patch(patch: Mapping[str, Any], workspace: Mapping[str, Any]) -> dict[str, Any]:
    diff = patch.get('diff') if isinstance(patch.get('diff'), Mapping) else {}
    return {
        'status': patch.get('status'),
        'workspace_ref': workspace.get('workspace_ref'),
        'diff': ''.join(str(value) for value in diff.values()),
    }


def candidate_rag_answer(case: Mapping[str, Any], service: Mapping[str, Any]) -> dict[str, Any]:
    target = {
        'target_chat_url': _text(service.get('service_url')),
        'algorithm_id': _text(service.get('algorithm_id')),
        'kb_id': _case_kb_id(case, service),
    }
    if service.get('status') != 'ready':
        return failed_rag_answer(case, {}, target, 'candidate_service_unavailable',
                                 _text((service.get('healthcheck') or {}).get('message')) or 'candidate not ready')
    health = _service_health(service)
    if health.get('status') != 'passed':
        return failed_rag_answer(case, {}, target, 'candidate_service_unhealthy',
                                 _text(health.get('message')) or 'candidate has no healthy instance')
    config = service.get('candidate_config') if isinstance(service.get('candidate_config'), Mapping) else {}
    target_config = dict(config) | {
        'target_chat_url': service.get('service_url'),
        'algorithm_id': service.get('algorithm_id'),
    }
    result = call_chat_answer(case, target_config, target['kb_id'])
    return _validate_candidate_route(result, service)


def candidate_judge_result(
    case: Mapping[str, Any],
    rag_answer: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    results: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            results.put(validate_judge_result(judge_answer(case, rag_answer, policy)))
        except Exception as exc:
            results.put(validate_judge_result(judge_contract_error(case, rag_answer, policy, str(exc))))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    timeout_s = _int_between(policy.get('judge_timeout_seconds'), 90, 1, 900)
    worker.join(timeout_s)
    if worker.is_alive():
        reason = f'candidate judge exceeded {timeout_s}s'
        return validate_judge_result(judge_contract_error(case, rag_answer, policy, reason))
    return results.get()


def compare_eval_detail_for_repair(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    skipped = candidate.get('total') and candidate.get('quality_counts', {}).get('skipped') == candidate.get('total')
    baseline_rows, candidate_rows = _rows_by_case(baseline.get('rows')), _rows_by_case(candidate.get('rows'))
    case_ids = list(dict.fromkeys([*baseline_rows, *candidate_rows, *baseline.get('case_ids', ()),
                                   *candidate.get('case_ids', ())]))
    before, after = _summary_metrics(baseline), _summary_metrics(candidate)
    delta = {key: round(after[key] - before[key], 4) for key in before}
    case_deltas = [_case_delta(case_id, baseline_rows.get(case_id), candidate_rows.get(case_id))
                   for case_id in case_ids]
    failures = list(candidate.get('execution_failures') or [])
    candidate_failed = bool(failures) or not (candidate.get('checks') or {}).get('ready')
    regressions = [row for row in case_deltas if row['outcome'] == 'regressed']
    guard = _goodcase_guard(case_deltas, baseline_rows)
    reasons = []
    if skipped:
        reasons.append('candidate evaluation skipped because repair patch is not verified')
    if candidate_failed:
        reasons.append('candidate evaluation produced execution failures')
    if regressions:
        reasons.append(f'{len(regressions)} case(s) regressed on overall_score')
    verdict = 'skipped' if skipped else 'candidate_eval_failed' if candidate_failed else 'review_candidate'
    return {
        'id': 'abtest.comparison',
        'status': 'skipped' if skipped else 'failed' if candidate_failed else 'completed',
        'verdict': verdict,
        'case_ids': case_ids,
        'case_count': len(case_ids),
        'metrics': {'baseline': before, 'candidate': after, 'delta': delta},
        'case_deltas': case_deltas,
        'goodcase_guard': guard,
        'policy': {'primary_metric': 'overall_score', 'guard_metrics': ['overall_score', 'answer_correctness']},
        'decision': {'status': verdict, 'primary_metric': 'overall_score', 'reasons': reasons},
        'reasons': reasons,
        'missing_metrics': [{'case_id': row['case_id'], 'outcome': row['outcome']} for row in case_deltas
                            if row['outcome'].startswith('missing_')],
        'data_warnings': [],
        'baseline': {'total': baseline.get('total', 0), 'quality_counts': dict(baseline.get('quality_counts') or {})},
        'candidate': {'total': candidate.get('total', 0),
                      'quality_counts': dict(candidate.get('quality_counts') or {})},
        'summary': {'metrics': {'baseline': before, 'candidate': after, 'delta': delta},
                    'case_deltas': case_deltas, 'goodcase_guard': guard, 'decision': verdict,
                    'case_count': len(case_ids), 'reasons': reasons},
    }


def _validate_candidate_route(answer: dict[str, Any], service: Mapping[str, Any]) -> dict[str, Any]:
    target = answer.get('target') if isinstance(answer.get('target'), Mapping) else {}
    expected, actual = _text(service.get('algorithm_id')), _text(target.get('routed_algorithm_id'))
    if expected and actual != expected:
        error_type = 'candidate_route_mismatch' if actual else 'candidate_route_missing'
        return answer | {'status': 'failed', 'chat_error': {
            'type': error_type,
            'message': f'expected {expected}, got {actual or "<missing>"}',
        }, 'evidence_status': 'failed'}
    return answer


def _service_health(service: Mapping[str, Any]) -> dict[str, Any]:
    try:
        admin_url = _text(service.get('router_admin_url'))
        algorithm_id = _text(service.get('algorithm_id'))
        return _healthcheck(_router_get(admin_url, algorithm_id, timeout_s=10))
    except Exception as exc:
        return {'status': 'failed', 'message': str(exc), 'healthy_instances': 0, 'instances': []}


def _wait_ready(admin_url: str, algorithm_id: str, timeout_s: int) -> Mapping[str, Any]:
    deadline, last = time.monotonic() + timeout_s, None
    while time.monotonic() <= deadline:
        last = _router_get(admin_url, algorithm_id, timeout_s=min(10, timeout_s))
        if last and _healthcheck(last)['status'] == 'passed':
            return last
        time.sleep(1.0)
    raise TimeoutError(f'candidate algorithm {algorithm_id} not healthy before timeout; last={last}')


def _router_get(admin_url: str, algorithm_id: str, *, timeout_s: float) -> Mapping[str, Any] | None:
    url = f'{admin_url}/inner/algorithm/{quote(algorithm_id)}'
    try:
        return _router_request('GET', url, timeout_s=timeout_s)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _router_request(
    method: str,
    url: str,
    *,
    body: Mapping[str, Any] | None = None,
    timeout_s: float,
) -> Mapping[str, Any]:
    with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
        response = client.request(method, url, json=body)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise ValueError(f'router response is not an object: {url}')
        return value


def _healthcheck(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    instances = list((detail or {}).get('instances') or [])
    healthy = [item for item in instances if isinstance(item, Mapping) and item.get('status') == 'healthy']
    ok = (detail or {}).get('status') == 'active' and bool(healthy)
    return {'status': 'passed' if ok else 'failed',
            'algorithm_status': (detail or {}).get('status'),
            'healthy_instances': len(healthy), 'instances': instances}


def _same_registration(existing: Mapping[str, Any], body: Mapping[str, Any]) -> bool:
    return existing.get('code_path') == body.get('code_path') and dict(existing.get('config') or {}) == body['config']


def _failed_service(algorithm_id: str, service_url: str, admin_url: str, code_path: str,
                    error_type: str, message: str) -> dict[str, Any]:
    return {'status': 'failed', 'service_kind': 'router_algorithm', 'algorithm_id': algorithm_id,
            'service_url': service_url, 'router_admin_url': admin_url, 'code_path': code_path,
            'healthcheck': {'status': 'failed', 'type': error_type, 'message': message}}


def _candidate_algorithm_id(config: Mapping[str, Any], patch: Mapping[str, Any], run_id: str) -> str:
    explicit = _text(config.get('algorithm_id'))
    if explicit:
        return _safe_id(explicit, 'evo_candidate')[:64]
    digest = hashlib.sha1(json.dumps({'workspace': patch.get('workspace_ref'), 'diff': patch.get('diff')},
                                     sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f'evo_{_safe_id(_text(config.get("thread_id") or run_id), "run")}_{digest}'[:64]


def _candidate_env(config: Mapping[str, Any], algorithm_id: str) -> dict[str, str]:
    kb_name = _text(config.get('agentic_kb_name') or os.getenv('LAZYMIND_AGENTIC_KB_NAME') or 'general_algo')
    env = {'LAZYMIND_ALGO_ID': _text(config.get('algo_id')) or kb_name,
           'LAZYMIND_AGENTIC_KB_NAME': kb_name,
           'LAZYMIND_ROUTER_ALGORITHM_ID': algorithm_id,
           'LAZYMIND_ENABLE_ROUTER': 'false',
           'LAZYMIND_ROUTER_CHILD_PROXIED_ONLY': 'true'}
    env.update({key: _text(os.getenv(key)) for key in ENV_PASSTHROUGH if _text(os.getenv(key))})
    extra = config.get('env') if isinstance(config.get('env'), Mapping) else {}
    env.update({_text(key): _text(value) for key, value in extra.items() if _text(key) and _text(value)})
    return env


def _code_path(config: Mapping[str, Any], patch: Mapping[str, Any]) -> str:
    if config.get('code_path'):
        return _text(config['code_path'])
    workspace = Path(_text(patch.get('workspace_ref'))).as_posix()
    if not workspace:
        raise ValueError('verified repair patch must provide workspace_ref or candidate_config.code_path')
    return f'{workspace.rstrip("/")}/lazymind/chat'


def _case_kb_id(case: Mapping[str, Any], service: Mapping[str, Any]) -> str:
    config = service.get('candidate_config') if isinstance(service.get('candidate_config'), Mapping) else {}
    by_case = config.get('case_metadata_by_id') if isinstance(config.get('case_metadata_by_id'), Mapping) else {}
    metadata = case.get('case_metadata') if isinstance(case.get('case_metadata'), Mapping) else {}
    prep = case.get('source_preparation') if isinstance(case.get('source_preparation'), Mapping) else {}
    source = prep.get('case_source') if isinstance(prep.get('case_source'), Mapping) else {}
    case_id = _text(case.get('id'))
    if isinstance(by_case.get(case_id), Mapping) and by_case[case_id].get('kb_id'):
        return _text(by_case[case_id]['kb_id'])
    return _text(metadata.get('kb_id') or source.get('kb_id') or config.get('kb_id') or service.get('kb_id'))


def _summary_metrics(summary: Mapping[str, Any]) -> dict[str, float]:
    metrics = summary.get('metrics') if isinstance(summary.get('metrics'), Mapping) else {}
    return {key: _float(metrics.get(key if key == 'correct_rate' else f'{key}_avg')) for key in COMPARE_METRICS}


def _rows_by_case(rows: object) -> dict[str, Mapping[str, Any]]:
    items = rows if isinstance(rows, (list, tuple)) else ()
    return {
        _text(row.get('case_id')): row
        for row in items
        if isinstance(row, Mapping) and _text(row.get('case_id'))
    }


def _case_delta(case_id: str, baseline: Mapping[str, Any] | None, candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    before, after = _row_metrics(baseline or {}), _row_metrics(candidate or {})
    delta = {key: round(after[key] - before[key], 4) for key in before}
    if not baseline:
        outcome = 'missing_baseline'
    elif not candidate:
        outcome = 'missing_candidate'
    elif candidate.get('failure_type') == 'infra_failure' or delta['overall_score'] < -0.0001:
        outcome = 'regressed'
    elif delta['overall_score'] > 0.0001:
        outcome = 'improved'
    else:
        outcome = 'unchanged'
    return {'case_id': case_id, 'outcome': outcome, 'before': before, 'after': after, 'delta': delta,
            'baseline_quality': _text((baseline or {}).get('quality_label')),
            'candidate_quality': _text((candidate or {}).get('quality_label'))}


def _row_metrics(row: Mapping[str, Any]) -> dict[str, float]:
    return {key: _float(row.get(key)) for key in COMPARE_METRICS if key != 'correct_rate'}


def _goodcase_guard(case_deltas: list[dict[str, Any]], baseline_rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    violations = [row for row in case_deltas
                  if baseline_rows.get(row['case_id'], {}).get('quality_label') == 'good'
                  and row['delta']['overall_score'] < -0.05]
    return {'status': 'failed' if violations else 'passed', 'violations': violations}


def _chat_url(value: object) -> str:
    url = _text(value or 'http://chat:8046/api/chat/stream')
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('target_chat_url must be an http(s) URL')
    return urlunparse((parsed.scheme, parsed.netloc, '/api/chat/stream', '', '', ''))


def _origin(value: object) -> str:
    parsed = urlparse(_text(value))
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('router_admin_url must be an http(s) URL')
    return urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value


def _safe_id(value: str, fallback: str) -> str:
    return SAFE_ID.sub('_', value).strip('._-') or fallback


def _text(value: object) -> str:
    return str(value or '').strip()


def _float(value: object) -> float:
    try:
        return round(float(value or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


def _int_between(value: object, default: int, low: int, high: int) -> int:
    return max(low, min(high, int(value if value not in (None, '') else default)))
