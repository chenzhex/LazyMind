from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unidiff import PatchSet

from evo.operations.public_contracts import RepairPatch, algo_id, dump_contract

from .candidate import validate_candidate_patch
from .decision import patch_base_decision, select_best_attempt
from .memory import patch_policy_check, patch_profile, repair_memory
from .opencode import run_opencode_streaming
from .trace import safe_emit, trace_cursor
from .validation import pre_validate
from .workspace import (
    algorithm_source_root,
    apply_diff,
    git,
    prepare_workspace,
    reset_workspace,
    source_fingerprint,
    workspace_diff,
    workspace_fingerprint,
    workspace_path,
)

DEFAULT_SOURCE = '/app/algorithm'
PROVIDER_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


def prepare_candidate_workspace(
    plan: Mapping[str, Any],
    repair_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if plan.get('status') != 'planned':
        return {'status': 'skipped', 'repair_plan_ref': _plan_ref(plan), 'workspace_kind': 'managed_worktree'}
    policy = _runtime_policy(plan, repair_policy)
    source = algorithm_source_root(policy.get('candidate_source_dir') or os.getenv('LAZYMIND_EVO_CHAT_SOURCE')
                                   or DEFAULT_SOURCE)
    workspace = workspace_path(policy, plan)
    objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    prepare_workspace(source, workspace, objective_hash)
    source_hash = source_fingerprint(source)['source_hash']
    return {
        'status': 'ready',
        'workspace_kind': 'managed_worktree',
        'workspace_ref': str(workspace),
        'source_dir': str(source),
        'source_hash': source_hash,
        'objective_hash': objective_hash,
        'git_head': git(workspace, 'rev-parse', '--verify', 'HEAD'),
        'repair_plan_ref': _plan_ref(plan),
    }


def run_repair_loop(workspace: Mapping[str, Any], cases: tuple[Mapping[str, Any], ...],
                    baseline_judges: tuple[Mapping[str, Any], ...], eval_policy: Mapping[str, Any],
                    candidate_config: Mapping[str, Any], repair_policy: Mapping[str, Any], ctx: Any,
                    plan: Mapping[str, Any] | None = None,
                    trace: Any | None = None,
                    ) -> dict[str, Any]:
    plan = plan if isinstance(plan, Mapping) else {}
    baseline_algo_id = next((text for judge in baseline_judges for text in (algo_id(judge),) if text), '')
    if workspace.get('status') != 'ready' or plan.get('status') != 'planned':
        return _early_result(trace, 'skipped', plan, workspace, 'repair plan is not runnable', baseline_algo_id)
    policy = _runtime_policy(plan, repair_policy)
    if not _text(workspace.get('workspace_ref')):
        return _early_result(trace, 'blocked', plan, workspace, 'missing candidate workspace_ref', baseline_algo_id)
    objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    try:
        root = Path(_text(workspace.get('workspace_ref'))).resolve()
        expected_root = workspace_path(policy, plan).resolve()
        fingerprint = workspace_fingerprint(root)
        workspace_head = git(root, 'rev-parse', '--verify', 'HEAD')
    except (OSError, RuntimeError, ValueError):
        return _early_result(trace, 'blocked', plan, workspace,
                             'candidate workspace artifact failed integrity check', baseline_algo_id)
    if (
        root != expected_root
        or fingerprint.get('source_hash') != workspace.get('source_hash')
        or fingerprint.get('source_dir') != workspace.get('source_dir')
        or fingerprint.get('objective_hash') != objective_hash
        or workspace_head != workspace.get('git_head')
        or not (root / 'lazymind' / 'chat' / 'app.py').exists()
    ):
        return _early_result(trace, 'blocked', plan, workspace,
                             'candidate workspace artifact failed integrity check', baseline_algo_id)
    case_map = {_text(case.get('id')): case for case in cases
                if isinstance(case, Mapping) and _text(case.get('id'))}
    baseline_map = {_text(judge.get('case_id')): judge for judge in baseline_judges
                    if isinstance(judge, Mapping) and _text(judge.get('case_id'))}
    if not case_map or not baseline_map:
        return _early_result(trace, 'blocked', plan, workspace,
                             'repair requires real cases and baseline judges', baseline_algo_id)
    opencode_config = _opencode_config_from_policy(policy)
    if not opencode_config:
        return _early_result(trace, 'blocked', plan, workspace,
                             'missing opencode model configuration', baseline_algo_id)
    attempts, base_diff, base_mode, best = [], '', 'baseline', {}
    budget = _int(policy.get('repair_attempt_budget'), 30, 1, 100)
    session_id = ''
    for attempt_no in range(1, budget + 1):
        safe_emit(trace, 'repair.attempt_started', status='started', attempt=attempt_no,
              payload={'budget': budget})
        reset_workspace(root)
        if base_diff:
            try:
                apply_diff(root, base_diff)
            except RuntimeError as exc:
                safe_emit(trace, 'repair.base_selected', status='failed', attempt=attempt_no,
                      payload={'mode': base_mode, 'reason': str(exc)[:500]})
                base_diff, base_mode = '', 'baseline'
                reset_workspace(root)
        safe_emit(trace, 'repair.base_selected', status='completed', attempt=attempt_no,
              payload={'mode': base_mode})
        base_profile = patch_profile(base_diff)
        task = _task_card(plan, workspace, attempt_no, attempts, base_profile, base_mode)
        run = run_opencode_streaming(
            workdir=str(root),
            prompt=json.dumps(task, ensure_ascii=False, indent=2),
            artifact_dir=root / '.evo_repair_logs' / 'opencode' / f'attempt_{attempt_no}',
            session_id=session_id,
            config=opencode_config,
            timeout_s=_int(policy.get('opencode_timeout_s') or os.getenv('LAZYMIND_EVO_CODE_TIMEOUT_S'), 900, 30, 7200),
            trace=trace,
            attempt=attempt_no,
        )
        session_id = run.session_id or session_id
        diff_info = workspace_diff(root)
        profile = patch_profile(diff_info['diff'])
        patch_policy = patch_policy_check(profile, attempts, base_profile)
        safe_emit(trace, 'verify.patch_policy_completed',
              status='completed' if patch_policy['status'] == 'passed' else 'failed',
              attempt=attempt_no, payload=patch_policy)
        pre = (
            _opencode_failure(run)
            if run.returncode or run.last_error
            else patch_policy
            if patch_policy['status'] != 'passed'
            else pre_validate(root, diff_info, plan, policy, trace, attempt_no)
        )
        candidate = (
            validate_candidate_patch(root, diff_info['diff'], plan, case_map, baseline_map, eval_policy,
                                     candidate_config, ctx, trace, attempt_no)
            if pre['status'] == 'passed'
            else {'status': 'skipped', 'accepted': False, 'reason': 'pre_validation_failed'}
        )
        delta = (
            dict(candidate['analysis_delta'])
            if isinstance(candidate.get('analysis_delta'), Mapping)
            else {'status': 'skipped', 'target_group_status': 'not_evaluated',
                  'recommended_action': 'rollback_to_baseline'}
        )
        decision = patch_base_decision(pre, candidate, delta, best)
        safe_emit(trace, 'repair.decision_completed', status='completed', attempt=attempt_no,
              payload={'action': decision.get('action'), 'reason': decision.get('reason')})
        attempt = {
            'attempt': attempt_no,
            'status': 'validated' if decision['action'] == 'accept_patch' else 'failed',
            'base': {'mode': base_mode},
            'opencode': {
                'returncode': run.returncode,
                'last_error': run.last_error,
            },
            'pre_validation': pre,
            'candidate_validation': candidate,
            'analysis_delta': delta,
            'patch_base_decision': decision,
            'files_changed': diff_info['files'],
            'diff': diff_info['diff'],
            'patch_profile': profile.as_dict(),
        }
        attempts.append(attempt)
        best = select_best_attempt(best, attempt)
        if decision['action'] == 'accept_patch':
            safe_emit(trace, 'repair.loop_completed', status='completed', terminal=True,
                  payload={'status': 'validated', 'attempt_count': len(attempts)})
            return _result('validated', plan, workspace, attempts, attempt, 'validated repair patch',
                           baseline_algo_id, trace_cursor(trace))
        if decision['action'] == 'blocked':
            reset_workspace(root)
            safe_emit(trace, 'repair.loop_completed', status='failed', terminal=True,
                  payload={'status': 'blocked', 'reason': decision.get('reason')})
            return _result('blocked', plan, workspace, attempts, {}, decision['reason'], baseline_algo_id,
                           trace_cursor(trace))
        base_mode = 'baseline'
        base_diff = diff_info['diff'] if decision['action'] == 'continue_current_patch' else ''
        if base_diff:
            base_mode = 'continued_patch'
        if decision['action'] == 'fork_from_best_attempt':
            base_diff = best.get('diff') or ''
            base_mode = 'fork_from_best_attempt' if base_diff else 'baseline'
    reset_workspace(root)
    safe_emit(trace, 'repair.loop_completed', status='failed', terminal=True,
          payload={'status': 'no_validated_patch', 'attempt_count': len(attempts)})
    return _result('no_validated_patch', plan, workspace, attempts, best, f'attempt budget exhausted: {budget}',
                   baseline_algo_id, trace_cursor(trace))


def build_verified_patch(run_id: str, loop: Mapping[str, Any]) -> dict[str, Any]:
    raw_diff = loop.get('winning_patch_diff')
    diff = raw_diff if isinstance(raw_diff, str) else str(raw_diff or '')
    if loop.get('status') != 'validated' or not diff.strip():
        status = 'blocked' if loop.get('status') == 'blocked' else 'no_patch'
        return dump_contract(RepairPatch, {
            'run_id': run_id,
            'algo_id': _text(loop.get('algo_id')),
            'candidate_algo_id': '',
            'status': status,
            'diff': {},
        })
    return dump_contract(RepairPatch, {
        'run_id': run_id,
        'algo_id': _text(loop.get('algo_id')),
        'candidate_algo_id': _text(loop.get('candidate_algo_id')),
        'status': 'verified',
        'diff': _diff_by_file(diff),
    })


def _early_result(
    trace: Any | None,
    status: str,
    plan: Mapping[str, Any],
    workspace: Mapping[str, Any],
    message: str,
    algo_id_value: str,
) -> dict[str, Any]:
    safe_emit(trace, 'repair.loop_completed', status='skipped' if status == 'skipped' else 'failed',
          terminal=True, payload={'status': status, 'reason': message})
    return _result(status, plan, workspace, [], {}, message, algo_id_value, trace_cursor(trace))


def _opencode_failure(run: Any) -> dict[str, Any]:
    error = run.last_error or {'type': 'process_failed', 'message': f'opencode exited with {run.returncode}'}
    return {
        'status': 'failed',
        'reason': f"opencode_failed:{_text(error.get('type'))}",
        'diff_scope': {},
        'hardcode_check': {},
        'commands': [],
        'opencode_error': error,
    }


def _task_card(plan: Mapping[str, Any], workspace: Mapping[str, Any], attempt: int,
               attempts: list[Mapping[str, Any]], base_profile: Any | None = None,
               base_mode: str = 'baseline') -> dict[str, Any]:
    brief = plan.get('brief') if isinstance(plan.get('brief'), Mapping) else {}
    memory = repair_memory(attempts, base_profile)
    return {
        'mode': 'lazyrag_trace_driven_repair_v1',
        'attempt': attempt,
        'objective': plan.get('objective'),
        'brief': brief,
        'workspace': {'path': workspace.get('workspace_ref'), 'source_dir': workspace.get('source_dir')},
        'selected_base': {
            'mode': base_mode,
            'patch_profile': base_profile.as_dict() if base_profile and base_profile.normalized_hash else {},
        },
        'repair_memory': memory,
        'previous_attempts': memory['recent_attempts'][-2:],
        'hard_constraints': [
            'Preserve selected_base.patch_profile when mode is not baseline.',
            'Do not repeat forbidden_patch_fingerprints or forbidden_edit_shape_hashes outside selected_base.',
            'If continuing from a selected base patch, add a new repair hypothesis; do not only retune carried edits.',
        ],
        'instructions': [
            'Patch exactly the selected function block. Do not choose another group.',
            'Inspect seed_files first and stay inside allowed_roots.',
            'Do not edit eval, analysis, dataset, tests, secrets, generated data, or vendored lazyllm.',
            'Make the smallest code change that addresses the trace-derived symptom.',
            'Run the requested verification command before stopping.',
        ],
        'stop_condition': 'Leave a minimal git diff in the workspace, or explain why no safe patch exists.',
    }


def _result(status: str, plan: Mapping[str, Any], workspace: Mapping[str, Any], attempts: list[Mapping[str, Any]],
            best: Mapping[str, Any], message: str, algo_id_value: str = '',
            trace_cursor: Mapping[str, Any] | None = None) -> dict[str, Any]:
    winner = best if _validated_attempt(best) else {}
    candidate = winner.get('candidate_validation') if isinstance(winner.get('candidate_validation'), Mapping) else {}
    service = candidate.get('service') if isinstance(candidate.get('service'), Mapping) else {}
    diff = winner.get('diff')
    return {
        'id': 'repair.loop_result',
        'status': status,
        'message': message,
        'algo_id': algo_id_value,
        'attempt_count': len(attempts),
        'files_changed': winner.get('files_changed') or [],
        'candidate_algo_id': _text(service.get('algorithm_id')),
        'winning_patch_diff': diff if isinstance(diff, str) else str(diff or ''),
        'selected_group': _group_summary(plan.get('selected_group')),
        'trace_cursor': dict(trace_cursor or {}),
    }


def _diff_by_file(diff: str) -> dict[str, str]:
    if not diff.strip():
        return {}
    lines = diff.splitlines(True)
    patches = PatchSet(lines)
    result = {}
    for patched in patches:
        source = _text(patched.source_file).removeprefix('a/')
        target = _text(patched.target_file).removeprefix('b/')
        path = source if target == '/dev/null' else target or source
        if path:
            result[path] = str(patched)
    return result


def _group_summary(value: object) -> dict[str, Any]:
    group = value if isinstance(value, Mapping) else {}
    return _pick(group, (
        'group_id',
        'function_block_id',
        'issue_type',
        'failure_mode',
        'badcase_count',
        'representative_case_id',
        'confidence_score',
        'candidate_files',
        'case_ids',
        'trace_cluster_id',
        'trace_signature',
    ))


def _plan_ref(plan: Mapping[str, Any]) -> dict[str, Any]:
    objective_hash = hashlib.sha1(
        json.dumps(plan.get('objective') or {}, sort_keys=True).encode()
    ).hexdigest()[:12]
    return {
        'status': _text(plan.get('status')),
        'objective_hash': objective_hash,
        'selected_group': _group_summary(plan.get('selected_group')),
    }


def _opencode_config_from_policy(policy: Mapping[str, Any]) -> dict[str, str]:
    llm_config = (
        policy.get('llm_config')
        if isinstance(policy.get('llm_config'), Mapping) else {}
    )
    role = {}
    value = llm_config.get('evo_llm')
    if isinstance(value, Mapping):
        role = value
    if not role:
        return {}
    model = _text(role.get('model'))
    base_url = _text(role.get('base_url')).rstrip('/')
    provider = _text(role.get('provider') or role.get('source')).lower()
    if not PROVIDER_NAME.fullmatch(provider):
        return {}
    if provider == 'qwen' and base_url == 'https://dashscope.aliyuncs.com':
        base_url = f'{base_url}/compatible-mode/v1'
    api_key = _text(role.get('api_key'))
    skip_auth = role.get('skip_auth') is True
    if not (provider and model and base_url and (api_key or skip_auth)):
        return {}
    return {
        'model': f'{provider}/{model}',
        'provider': provider,
        'provider_model': model,
        'provider_label': provider,
        'base_url': base_url,
        'api_key': api_key,
        'skip_auth': 'true' if skip_auth else '',
    }


def _runtime_policy(plan: Mapping[str, Any], repair_policy: Mapping[str, Any] | None) -> dict[str, Any]:
    safe = plan.get('policy') if isinstance(plan.get('policy'), Mapping) else {}
    raw = repair_policy if isinstance(repair_policy, Mapping) else {}
    safe_keys = {
        'allowed_roots',
        'blocked_roots',
        'target_case_budget',
        'neighbor_case_budget',
        'goodcase_guard_budget',
        'cross_block_guard_budget',
        'evidence_case_budget',
        'seed_file_budget',
    }
    runtime_keys = {
        'candidate_source_dir',
        'candidate_workdir',
        'workspace_namespace',
        'thread_id',
        'verification_commands',
        'repair_attempt_budget',
        'opencode_timeout_s',
        'max_patch_bytes',
        'llm_config',
    }
    return {
        **{key: safe[key] for key in safe_keys if key in safe},
        **{key: raw[key] for key in runtime_keys if key in raw},
    }


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _text(value: Any) -> str:
    return str(value or '').strip()


def _pick(value: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}


def _validated_attempt(attempt: Mapping[str, Any]) -> bool:
    candidate = attempt.get('candidate_validation') if isinstance(attempt.get('candidate_validation'), Mapping) else {}
    pre = attempt.get('pre_validation') if isinstance(attempt.get('pre_validation'), Mapping) else {}
    analysis = attempt.get('analysis_delta') if isinstance(attempt.get('analysis_delta'), Mapping) else {}
    comparison = candidate.get('comparison') if isinstance(candidate.get('comparison'), Mapping) else {}
    return (
        attempt.get('status') == 'validated'
        and bool(_text(attempt.get('diff')))
        and pre.get('status') == 'passed'
        and candidate.get('accepted') is True
        and comparison.get('status') == 'completed'
        and analysis.get('status') == 'completed'
    )
