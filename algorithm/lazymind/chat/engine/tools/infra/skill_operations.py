from __future__ import annotations

import posixpath
from typing import Any, Dict, List, Mapping

from typing_extensions import TypedDict


class SkillEditOperation(TypedDict, total=False):
    """JSON edit operation applied to a skill package."""

    op: str
    path: str
    old: str
    new: str
    old_text: str
    new_text: str
    content: str
    replace_all: bool


def normalize_skill_package_path(path: str | None, *, default: str = 'SKILL.md') -> str:
    raw = str(path or default).strip()
    if not raw or raw.startswith('/') or '\\' in raw:
        raise ValueError('operation path must be a non-empty relative POSIX path.')
    parts = raw.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError("operation path must not contain empty, '.', or '..' segments.")
    normalized = posixpath.normpath(raw)
    if normalized in ('', '.') or normalized == '..' or normalized.startswith('../'):
        raise ValueError('operation path must stay inside the skill package.')
    return normalized


def apply_skill_package_operations(
    current_files: Mapping[str, str],
    operations: List[SkillEditOperation],
) -> tuple[dict[str, str], list[Dict[str, Any]]]:
    from lazymind.rewrite.base import UnprocessableContentError

    if not operations:
        raise UnprocessableContentError("action='modify' requires a non-empty 'operations' list.")

    files = {
        normalize_skill_package_path(path): content
        for path, content in current_files.items()
    }
    if 'SKILL.md' not in files:
        raise UnprocessableContentError('Skill package must contain SKILL.md.')

    payload: list[Dict[str, Any]] = []
    changed = False
    for raw_op in operations:
        op = dict(raw_op)
        op_name = str(op.get('op') or '').strip()
        path = normalize_skill_package_path(op.get('path'))
        if op_name == 'patch_file':
            if path not in files:
                raise UnprocessableContentError(f'patch_file target does not exist: {path}')
            old = op.get('old_text', op.get('old'))
            new = op.get('new_text', op.get('new'))
            if not isinstance(old, str):
                raise UnprocessableContentError("patch_file requires a string field 'old_text'.")
            if not isinstance(new, str):
                raise UnprocessableContentError("patch_file requires a string field 'new_text'.")
            if old == '':
                raise UnprocessableContentError("patch_file requires non-empty 'old_text'.")
            current = files[path]
            if old not in current:
                raise UnprocessableContentError(f'patch_file could not find old_text in {path}.')
            replace_count = -1 if bool(op.get('replace_all')) else 1
            edited = current.replace(old, new, replace_count)
            if edited != current:
                files[path] = edited
                changed = True
            payload.append({
                'op': 'patch_file',
                'path': path,
                'old_text': old,
                'new_text': new,
                'replace_all': bool(op.get('replace_all')),
            })
            continue

        if op_name == 'write_file':
            content = op.get('content')
            if not isinstance(content, str):
                raise UnprocessableContentError("write_file requires a string field 'content'.")
            if files.get(path) != content:
                files[path] = content
                changed = True
            payload.append({'op': 'write_file', 'path': path, 'content': content})
            continue

        if op_name == 'delete_file':
            if path == 'SKILL.md':
                raise UnprocessableContentError('SKILL.md cannot be deleted.')
            if path not in files:
                raise UnprocessableContentError(f'delete_file target does not exist: {path}')
            del files[path]
            changed = True
            payload.append({'op': 'delete_file', 'path': path})
            continue

        raise UnprocessableContentError(
            f"Unsupported skill operation {op_name!r}; expected 'patch_file', 'write_file', or 'delete_file'."
        )

    if not changed:
        raise UnprocessableContentError(
            'Edited skill package is unchanged from current package. '
            'A review row must contain at least one real content change.'
        )
    if 'SKILL.md' not in files:
        raise UnprocessableContentError('Skill package must contain SKILL.md.')
    return files, payload
