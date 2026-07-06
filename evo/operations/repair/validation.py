from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_VERIFY = ('python -m compileall -q lazymind/chat',)
PATCH_BYTE_LIMIT = 64 * 1024
SECRET_LITERAL = re.compile(
    r'(?i)[\'"]?(api[_-]?key|token|secret|password|authorization)[\'"]?\s*[:=]\s*'
    r'([\'"]?)(?!<redacted>|unused\b|os\.getenv\b|getenv\b)[A-Za-z0-9._~+/=-]{8,}\2'
)


def pre_validate(
    root: Path,
    diff_info: Mapping[str, Any],
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    trace: Any | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    _emit(trace, 'verify.pre_validation_started', status='started', attempt=attempt)
    diff, files = diff_info.get('diff') or '', list(diff_info.get('files') or [])
    if not diff.strip():
        _emit(trace, 'verify.pre_validation_completed', status='failed', attempt=attempt,
              payload={'reason': 'empty_diff'})
        return {'status': 'failed', 'reason': 'empty_diff', 'diff_scope': {}, 'commands': []}
    scope = _diff_scope(files, plan)
    _emit(trace, 'verify.diff_scope_completed', status='completed' if scope['status'] == 'passed' else 'failed',
          attempt=attempt, payload=scope)
    hardcode = _hardcode_check(diff, plan)
    _emit(trace, 'verify.hardcode_check_completed',
          status='completed' if hardcode['status'] == 'passed' else 'failed', attempt=attempt, payload=hardcode)
    patch_safety = _patch_safety_check(diff, policy)
    if scope['status'] != 'passed' or hardcode['status'] != 'passed' or patch_safety['status'] != 'passed':
        reason = next(item['reason'] for item in (scope, hardcode, patch_safety) if item['status'] != 'passed')
        _emit(trace, 'verify.pre_validation_completed', status='failed', attempt=attempt,
              payload={'reason': reason})
        return {'status': 'failed', 'reason': reason, 'diff_scope': scope, 'hardcode_check': hardcode,
                'patch_safety': patch_safety, 'commands': []}
    commands = _verify(root, policy, trace, attempt)
    status = (
        'passed'
        if scope['status'] == hardcode['status'] == commands['status'] == 'passed'
        else 'failed'
    )
    reason = '' if status == 'passed' else next(
        item['reason'] for item in (scope, hardcode, commands) if item['status'] != 'passed'
    )
    _emit(trace, 'verify.pre_validation_completed', status='completed' if status == 'passed' else 'failed',
          attempt=attempt, payload={'outcome': status, 'reason': reason})
    return {'status': status, 'reason': reason, 'diff_scope': scope, 'hardcode_check': hardcode,
            'patch_safety': patch_safety, 'commands': commands['results']}


def _diff_scope(files: list[str], plan: Mapping[str, Any]) -> dict[str, Any]:
    brief = plan.get('brief') if isinstance(plan.get('brief'), Mapping) else {}
    violations: list[Any] = []
    allowed = _scope_roots(brief.get('allowed_roots'), violations)
    blocked = _scope_roots(brief.get('blocked_roots'), violations)
    file_paths = [(path, _relative_path(path)) for path in files]
    for path, normalized in file_paths:
        in_allowed = any(normalized == root or normalized.startswith(f'{root}/') for root in allowed)
        in_blocked = any(normalized == root or normalized.startswith(f'{root}/') for root in blocked)
        if not normalized or not in_allowed or in_blocked:
            violations.append(path)
    target_files = set(_scope_roots(brief.get('target_files') or brief.get('seed_files'), violations))
    if target_files:
        violations.extend(path for path, normalized in file_paths if normalized not in target_files)
    return {'status': 'passed' if not violations else 'failed', 'reason': 'diff_scope_violation',
            'violations': violations, 'allowed_roots': allowed, 'blocked_roots': blocked}


def _hardcode_check(diff: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    case_ids = set((plan.get('objective') or {}).get('validation_case_ids') or [])
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    hits = sorted(case_id for case_id in case_ids if case_id and case_id in added)
    return {'status': 'passed' if not hits else 'failed', 'reason': 'hard_coded_validation_case_id', 'hits': hits}


def _patch_safety_check(diff: str, policy: Mapping[str, Any]) -> dict[str, Any]:
    limit = _int(policy.get('max_patch_bytes'), PATCH_BYTE_LIMIT, 4096, 2 * 1024 * 1024)
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    size = len(diff.encode('utf-8'))
    leaked = sorted({match.group(1).lower() for match in SECRET_LITERAL.finditer(added)})
    reason = 'patch_too_large' if size > limit else 'secret_literal_in_patch' if leaked else ''
    return {'status': 'failed' if reason else 'passed', 'reason': reason, 'bytes': size,
            'limit': limit, 'secret_keys': leaked}


def _verify(
    root: Path,
    policy: Mapping[str, Any],
    trace: Any | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    results = []
    raw_commands = policy.get('verification_commands')
    commands = (
        raw_commands
        if isinstance(raw_commands, (list, tuple))
        else (() if raw_commands in (None, '') else (raw_commands,))
    )
    for raw in commands or DEFAULT_VERIFY:
        command = shlex.split(raw) if isinstance(raw, str) else [str(item) for item in raw]
        label = ' '.join(command[:4])
        _emit(trace, 'verify.command_started', status='started', attempt=attempt, payload={'command': label})
        try:
            done = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=120, check=False)
            results.append({'command': command, 'returncode': done.returncode, 'stdout': done.stdout[-2000:],
                            'stderr': done.stderr[-2000:]})
        except Exception as exc:
            results.append({'command': command, 'returncode': None, 'stdout': '', 'stderr': str(exc),
                            'error_type': type(exc).__name__})
            _emit(trace, 'verify.command_completed', status='failed', attempt=attempt,
                  payload={'command': label, 'error_type': type(exc).__name__})
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
        _emit(trace, 'verify.command_completed', status='completed' if done.returncode == 0 else 'failed',
              attempt=attempt, payload={'command': label, 'returncode': done.returncode})
        if results[-1]['returncode'] != 0:
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
    return {'status': 'passed', 'reason': '', 'results': results}


def _emit(trace: Any | None, event_type: str, **kwargs: Any) -> None:
    if trace is None:
        return
    try:
        trace.emit(event_type, **kwargs)
    except Exception:
        pass


def _relative_path(value: Any) -> str:
    raw = str(value or '').strip()
    parts = raw.strip('/').split('/')
    if (
        not raw
        or raw.startswith('/')
        or '\\' in raw
        or any(part in {'', '.', '..'} for part in parts)
    ):
        return ''
    return PurePosixPath(raw).as_posix()


def _scope_roots(values: Any, violations: list[Any]) -> list[str]:
    roots = []
    for value in values or []:
        if path := _relative_path(value):
            roots.append(path)
        else:
            violations.append(str(value))
    return roots


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))
