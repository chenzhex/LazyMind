from __future__ import annotations

import hashlib
import ast
import json
import keyword
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unidiff import PatchSet

IDENT = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*\b')
ASSIGN = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*=')


@dataclass(frozen=True)
class PatchProfile:
    normalized_hash: str = ''
    edit_shape_hash: str = ''
    edit_locations: tuple[str, ...] = ()
    normalized_edits: tuple[str, ...] = ()
    strategy_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            'normalized_hash': self.normalized_hash,
            'edit_shape_hash': self.edit_shape_hash,
            'edit_locations': list(self.edit_locations),
            'normalized_edits': list(self.normalized_edits),
            'strategy_keys': list(self.strategy_keys),
        }


def patch_profile(diff: str) -> PatchProfile:
    text = str(diff or '')
    if not text.strip():
        return PatchProfile()
    changes = _changed_lines(text)
    if not changes:
        return PatchProfile()
    normalized = tuple(
        f'{path}:{loc}:{op}:{_normalize_line(line)}'
        for path, loc, op, line in changes
        if _normalize_line(line)
    )
    strategy_lines = [(path, loc, line) for path, loc, op, line in changes if op == '+' and line.strip()]
    if not strategy_lines:
        strategy_lines = [(path, loc, line) for path, loc, _op, line in changes if line.strip()]
    strategies = tuple(sorted({key for path, loc, line in strategy_lines for key in _strategy_keys(path, loc, line)}))
    shape = tuple(f'{path}:{loc}:{_line_shape(line)}' for path, loc, _op, line in changes if _line_shape(line))
    return PatchProfile(
        normalized_hash=_hash(json.dumps(normalized, sort_keys=True, ensure_ascii=False)),
        edit_shape_hash=_hash(json.dumps(shape, sort_keys=True, ensure_ascii=False)),
        edit_locations=tuple(sorted({f'{path}@{loc}' for path, loc, _op, _line in changes})),
        normalized_edits=normalized,
        strategy_keys=strategies,
    )


def patch_policy_check(
    profile: PatchProfile,
    attempts: list[Mapping[str, Any]],
    base_profile: PatchProfile | None = None,
) -> dict[str, Any]:
    if not profile.normalized_hash:
        return {'status': 'passed', 'reason': '', 'patch_profile': profile.as_dict()}
    base = base_profile or PatchProfile()
    memory = repair_memory(attempts, base)
    forbidden_hashes = set(memory['forbidden_patch_fingerprints'])
    forbidden_shapes = set(memory['forbidden_edit_shape_hashes'])
    current_strategies = set(profile.strategy_keys)
    base_strategies = set(base.strategy_keys)
    current_edits = set(profile.normalized_edits)
    base_edits = set(base.normalized_edits)
    reason = ''
    detail: dict[str, Any] = {}
    if base.normalized_hash and profile.normalized_hash == base.normalized_hash:
        reason = 'no_incremental_change_from_selected_base'
    elif base_edits and not base_edits <= current_edits:
        reason = 'selected_base_not_preserved'
        detail = {'missing_base_edits': sorted(base_edits - current_edits)[:8]}
    elif profile.normalized_hash in forbidden_hashes and profile.normalized_hash != base.normalized_hash:
        reason = 'repeated_forbidden_patch'
        detail = {'patch_fingerprint': profile.normalized_hash}
    elif profile.edit_shape_hash in forbidden_shapes and profile.edit_shape_hash != base.edit_shape_hash:
        reason = 'repeated_forbidden_edit_shape'
        detail = {'edit_shape_hash': profile.edit_shape_hash}
    elif base_strategies and current_strategies and current_strategies <= base_strategies:
        reason = 'repeated_carried_strategy_without_new_hypothesis'
        detail = {'strategy_keys': sorted(current_strategies)[:8]}
    return {
        'status': 'failed' if reason else 'passed',
        'reason': reason,
        'patch_profile': profile.as_dict(),
        'base_patch_profile': base.as_dict() if base.normalized_hash else {},
        **detail,
    }


def repair_memory(attempts: list[Mapping[str, Any]], selected_base: PatchProfile | None = None) -> dict[str, Any]:
    reasons = Counter(_decision_field(attempt, 'reason') for attempt in attempts)
    reasons.pop('', None)
    forbidden_hashes: set[str] = set()
    forbidden_shapes: set[str] = set()
    base = selected_base or PatchProfile()
    recent = [_attempt_summary(attempt) for attempt in attempts[-5:]]
    for attempt in attempts:
        profile = _attempt_profile(attempt)
        if attempt.get('status') != 'validated' and profile.normalized_hash:
            forbidden_hashes.add(profile.normalized_hash)
            forbidden_shapes.add(profile.edit_shape_hash)
    if base.normalized_hash:
        forbidden_hashes.discard(base.normalized_hash)
        forbidden_shapes.discard(base.edit_shape_hash)
    return {
        'attempt_count': len(attempts),
        'failure_reason_counts': dict(reasons),
        'recent_attempts': recent,
        'repeat_failures': [reason for reason, count in reasons.items() if count >= 2],
        'forbidden_patch_fingerprints': sorted(forbidden_hashes),
        'forbidden_edit_shape_hashes': sorted(forbidden_shapes),
        'forbidden_strategy_keys': [],
    }


def _attempt_summary(attempt: Mapping[str, Any]) -> dict[str, Any]:
    profile = _attempt_profile(attempt)
    return {
        'attempt': attempt.get('attempt'),
        'action': _decision_field(attempt, 'action'),
        'reason': _decision_field(attempt, 'reason'),
        'target_group_status': _nested(attempt, 'analysis_delta', 'target_group_status'),
        'target_remaining_delta': _nested(attempt, 'analysis_delta', 'target_remaining_delta'),
        'new_group_count': _nested(attempt, 'analysis_delta', 'new_group_count'),
        'files_changed': attempt.get('files_changed') or [],
        'patch_fingerprint': profile.normalized_hash,
        'strategy_keys': list(profile.strategy_keys)[:8],
    }


def _attempt_profile(attempt: Mapping[str, Any]) -> PatchProfile:
    raw = attempt.get('patch_profile')
    if isinstance(raw, Mapping):
        return PatchProfile(
            normalized_hash=str(raw.get('normalized_hash') or ''),
            edit_shape_hash=str(raw.get('edit_shape_hash') or ''),
            edit_locations=tuple(str(item) for item in raw.get('edit_locations') or ()),
            normalized_edits=tuple(str(item) for item in raw.get('normalized_edits') or ()),
            strategy_keys=tuple(str(item) for item in raw.get('strategy_keys') or ()),
        )
    return patch_profile(str(attempt.get('diff') or ''))


def _changed_lines(diff: str) -> tuple[tuple[str, str, str, str], ...]:
    patches = PatchSet(diff.splitlines(True))
    changes: list[tuple[str, str, str, str]] = []
    for patched in patches:
        path = _patch_path(str(patched.source_file), str(patched.target_file))
        for hunk in patched:
            loc = f'{hunk.source_start}:{hunk.target_start}'
            for line in hunk:
                if line.is_added or line.is_removed:
                    changes.append((path, loc, '+' if line.is_added else '-', line.value.rstrip('\n')))
    return tuple(changes)


def _patch_path(source: str, target: str) -> str:
    source = source.removeprefix('a/')
    target = target.removeprefix('b/')
    return source if target == '/dev/null' else target or source


def _normalize_line(line: str) -> str:
    return ' '.join(str(line or '').split())


def _strategy_keys(path: str, loc: str, line: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(line.strip())
    except SyntaxError:
        tree = None
    if tree is not None:
        keywords = {
            keyword.arg for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg
        }
        if keywords:
            return tuple(f'{path}@{loc}:assign:{name}' for name in sorted(keywords))
    names = [name for name in ASSIGN.findall(line) if not keyword.iskeyword(name)]
    if names:
        return tuple(f'{path}@{loc}:assign:{name}' for name in sorted(set(names)))
    names = [name for name in IDENT.findall(line) if not keyword.iskeyword(name)]
    if names:
        return (f'{path}@{loc}:ids:{",".join(sorted(set(names))[:6])}',)
    return (f'{path}@{loc}:shape:{_hash(_normalize_line(line))[:12]}',)


def _line_shape(line: str) -> str:
    names = [name for name in IDENT.findall(line) if not keyword.iskeyword(name)]
    return ','.join(sorted(set(names))[:8]) or _hash(_normalize_line(line))[:12]


def _decision_field(attempt: Mapping[str, Any], name: str) -> str:
    decision = attempt.get('patch_base_decision') if isinstance(attempt.get('patch_base_decision'), Mapping) else {}
    return str(decision.get(name) or '').strip()


def _nested(attempt: Mapping[str, Any], key: str, name: str) -> Any:
    value = attempt.get(key)
    return value.get(name) if isinstance(value, Mapping) else None


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode('utf-8')).hexdigest()[:16]
