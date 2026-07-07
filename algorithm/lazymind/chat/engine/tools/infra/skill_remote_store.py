from __future__ import annotations

from typing import Dict, Mapping

from lazymind.chat.integrations.remote_fs import RemoteFS
from lazymind.chat.engine.tools.infra.skill_operations import normalize_skill_package_path


def skill_package_dir(category: str, name: str) -> str:
    return f'remote://skills/{category}/{name}'


def _relative_to_package(package_dir: str, path: str) -> str:
    prefix = package_dir.rstrip('/') + '/'
    if path.startswith(prefix):
        return normalize_skill_package_path(path[len(prefix):])
    return normalize_skill_package_path(path.rsplit('/', 1)[-1])


def replace_skill_package_files(
    category: str,
    name: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    fs: RemoteFS,
) -> Dict[str, list[str]]:
    package_dir = skill_package_dir(category, name)
    before_paths = set(before)
    after_paths = set(after)
    deleted = sorted(before_paths - after_paths)
    written = sorted(path for path in after_paths if before.get(path) != after.get(path))
    for rel_path in deleted:
        fs.rm(f'{package_dir}/{normalize_skill_package_path(rel_path)}', recursive=False)
    for rel_path in written:
        fs.write(f'{package_dir}/{normalize_skill_package_path(rel_path)}', after[rel_path])
    return {'written': written, 'deleted': deleted}


def list_skill_files(category: str, name: str, *, fs: RemoteFS) -> Dict[str, str]:
    package_dir = skill_package_dir(category, name)
    files: Dict[str, str] = {}

    def walk(path: str) -> None:
        for entry in fs.ls(path, detail=True):
            entry_name = str((entry or {}).get('name') or '').strip()
            if not entry_name:
                continue
            entry_type = str((entry or {}).get('type') or 'file').strip()
            if entry_type in ('directory', 'dir'):
                walk(entry_name)
                continue
            rel_path = _relative_to_package(package_dir, entry_name)
            with fs.open(f'{package_dir}/{rel_path}', 'r', encoding='utf-8', errors='replace') as fh:
                files[rel_path] = fh.read()

    walk(package_dir)
    return files


def rename_skill_package(
    old_category: str,
    old_name: str,
    new_category: str,
    new_name: str,
    *,
    skill_content: str,
    fs: RemoteFS,
) -> dict:
    old_path = skill_package_dir(old_category, old_name)
    new_path = skill_package_dir(new_category, new_name)
    fs.move(old_path, new_path, recursive=True)
    fs.write(f'{new_path}/SKILL.md', skill_content)
    return {
        'persisted': 'remote_fs',
        'path': new_path,
        'old_name': old_name,
        'old_category': old_category,
        'name': new_name,
        'category': new_category,
        'action': 'rename',
    }


def create_remote_skill(category: str, name: str, content: str, *, fs: RemoteFS) -> dict:
    package_dir = skill_package_dir(category, name)
    path = f'{package_dir}/SKILL.md'
    fs.mkdir(package_dir, create_parents=True)
    fs.write(path, content)
    return {
        'persisted': 'remote_fs',
        'path': path,
        'name': name,
        'category': category,
        'action': 'create',
    }


def remove_remote_skill(category: str, name: str, *, fs: RemoteFS) -> dict:
    path = skill_package_dir(category, name)
    fs.trash(path)
    return {
        'persisted': 'remote_fs',
        'deleted': True,
        'path': path,
        'name': name,
        'category': category,
        'action': 'remove',
    }
