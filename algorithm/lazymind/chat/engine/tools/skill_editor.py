from typing import Any, Dict, List, Optional

import lazyllm

from lazymind.chat.integrations.remote_fs import RemoteFS
from lazymind.chat.engine.tools.infra import (
    create_remote_skill,
    list_skill_files,
    normalize_skill_category,
    remove_remote_skill,
    rename_skill_package,
    replace_skill_package_files,
    resolve_skill_editor_identity,
    rewrite_skill_identity,
    skill_identity_from_content,
    tool_error,
    tool_success,
    validate_skill_content,
    validate_skill_name,
)
from lazymind.chat.engine.tools.infra.skill_operations import (
    SkillEditOperation,
    apply_skill_package_operations,
)


class SkillEditorToolGroup:
    """Create, modify, rename, and remove reusable skill packages."""

    __public_apis__ = ['create_skill', 'modify_skill', 'rename_skill', 'remove_skill']

    def __init__(self, remote_fs: Optional[RemoteFS] = None):
        self.remote_fs = remote_fs or RemoteFS()

    def create_skill(self, name: str, category: Optional[str], content: str) -> Dict[str, Any]:
        """Create a new reusable skill from full SKILL.md content.

        The SKILL.md YAML frontmatter must include name, category, and
        description. Both name and category are path segments; category must be
        a single segment such as "engineering" or "coding". You may also pass
        name as "category/name" and omit category.

        Args:
            name: Skill name, or "category/name" when category is omitted.
            category: Skill category directory used for category/name/SKILL.md.
            content: Full SKILL.md content, including YAML frontmatter.
        """
        lazyllm.LOG.info(
            '[create_skill] called '
            f'name={name!r} category={category!r} content_len={len(content) if content else 0}'
        )
        resolved = resolve_skill_editor_identity(name, category, 'create_skill')
        if resolved.get('error'):
            return tool_error('create_skill', resolved['error'])
        normalized_category = resolved['category']
        name = resolved['name']
        lazyllm.LOG.info(f'[create_skill] lookup category={normalized_category!r} name={name!r}')

        content_error = validate_skill_content(content or '')
        if content_error:
            return tool_error(
                'create_skill',
                content_error,
                log_message=f'[create_skill] fail reason={content_error!r}',
            )
        content_category, content_name = skill_identity_from_content(content or '')
        if content_category != normalized_category or content_name != name:
            return tool_error(
                'create_skill',
                'SKILL.md frontmatter name/category must match the tool name/category for create.'
            )
        try:
            create_remote_skill(content_category, content_name, content or '', fs=self.remote_fs)
        except Exception as exc:
            return tool_error('create_skill', f'Failed to create skill package: {exc}')
        return tool_success('create_skill', {
            'status': 'created',
            'message': 'Skill was created and is now active.',
        })

    def modify_skill(
        self,
        name: str,
        category: Optional[str],
        operations: List[SkillEditOperation],
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Modify files inside an existing reusable skill package.

        Use this when a skill package is outdated, incomplete, or wrong.
        Operations are applied to package files, then SKILL.md frontmatter is
        validated. Do not change skill name or category here; use rename_skill.

        Args:
            name: Skill name, or "category/name" when category is omitted.
            category: Skill category directory used for category/name/SKILL.md.
            operations: Ordered JSON file edit operations. Supported operations:

                - ``{"op": "patch_file", "path": "SKILL.md", "old_text": "...", "new_text": "..."}``:
                  replace text in one package file. ``path`` defaults to SKILL.md.
                - ``{"op": "write_file", "path": "references/api.md", "content": "..."}``:
                  create or overwrite one package file.
                - ``{"op": "delete_file", "path": "examples/old.md"}``:
                  delete one supporting file. SKILL.md cannot be deleted.
            reason: Short summary of why this package is being modified.
        """
        lazyllm.LOG.info(
            '[modify_skill] called '
            f'name={name!r} category={category!r} operations_count={len(operations) if operations else 0}'
        )
        resolved = resolve_skill_editor_identity(name, category, 'modify_skill')
        if resolved.get('error'):
            return tool_error('modify_skill', resolved['error'])
        normalized_category = resolved['category']
        name = resolved['name']
        lazyllm.LOG.info(f'[modify_skill] lookup category={normalized_category!r} name={name!r}')

        if not operations:
            return tool_error('modify_skill', 'modify_skill requires a non-empty operations list.')
        try:
            from lazymind.rewrite.base import UnprocessableContentError

            current_files = list_skill_files(normalized_category, name, fs=self.remote_fs)
            edited_files, operation_payload = apply_skill_package_operations(
                current_files,
                operations,
            )
        except UnprocessableContentError as exc:
            return tool_error('modify_skill', str(exc))
        except Exception as exc:
            return tool_error('modify_skill', f'Failed to load or edit skill package: {exc}')

        edited_content = edited_files['SKILL.md']
        content_error = validate_skill_content(edited_content)
        if content_error:
            return tool_error('modify_skill', content_error)
        edited_category, edited_name = skill_identity_from_content(edited_content)
        if edited_category != normalized_category or edited_name != name:
            return tool_error(
                'modify_skill',
                'modify_skill cannot change SKILL.md frontmatter name/category; use rename_skill.'
            )
        try:
            change_set = replace_skill_package_files(
                normalized_category,
                name,
                current_files,
                edited_files,
                fs=self.remote_fs,
            )
        except Exception as exc:
            return tool_error('modify_skill', f'Failed to persist skill package changes: {exc}')

        result = {
            'status': 'modified',
            'message': 'Skill package was modified and is now active.',
        }
        touched_files = sorted({op.get('path', 'SKILL.md') for op in operation_payload})
        result['touched_files'] = touched_files
        result['written_files'] = change_set['written']
        result['deleted_files'] = change_set['deleted']
        operation_paths = sorted({str(op.get('path') or 'SKILL.md') for op in operation_payload})
        result['summary'] = (
            reason
            or f'skill_editor package operations: {len(operation_payload)} op(s), files={", ".join(operation_paths)}'
        )
        return tool_success('modify_skill', result)

    def rename_skill(
        self,
        name: str,
        category: Optional[str],
        new_name: str,
        new_category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Rename or move an existing reusable skill package.

        This moves the package and rewrites SKILL.md frontmatter name/category.
        Use this instead of modify_skill whenever the skill identity changes.

        Args:
            name: Current skill name, or "category/name" when category is omitted.
            category: Current skill category.
            new_name: New skill name.
            new_category: New skill category. If omitted, current category is kept.
        """
        lazyllm.LOG.info(
            '[rename_skill] called '
            f'name={name!r} category={category!r} new_name={new_name!r} new_category={new_category!r}'
        )
        resolved = resolve_skill_editor_identity(name, category, 'rename_skill')
        if resolved.get('error'):
            return tool_error('rename_skill', resolved['error'])
        normalized_category = resolved['category']
        name = resolved['name']
        lazyllm.LOG.info(f'[rename_skill] lookup category={normalized_category!r} name={name!r}')

        target_name = str(new_name or '').strip()
        name_error = validate_skill_name(target_name)
        if name_error:
            return tool_error('rename_skill', f'new_name is invalid: {name_error}')
        target_category = normalize_skill_category(new_category if new_category is not None else normalized_category)
        if not target_category:
            return tool_error(
                'rename_skill',
                f'new_category {new_category!r} is invalid; it must be a single ASCII-safe path segment.'
            )
        if target_category == normalized_category and target_name == name:
            return tool_error('rename_skill', 'rename_skill requires a different new_name or new_category.')

        try:
            current_files = list_skill_files(normalized_category, name, fs=self.remote_fs)
            skill_content = current_files.get('SKILL.md') or ''
            renamed_content = rewrite_skill_identity(skill_content, target_category, target_name)
        except Exception as exc:
            return tool_error('rename_skill', f'Failed to prepare skill rename: {exc}')
        content_error = validate_skill_content(renamed_content)
        if content_error:
            return tool_error('rename_skill', content_error)

        try:
            rename_skill_package(
                normalized_category,
                name,
                target_category,
                target_name,
                skill_content=renamed_content,
                fs=self.remote_fs,
            )
        except Exception as exc:
            return tool_error('rename_skill', f'Failed to rename skill package: {exc}')

        payload = {
            'old': {'category': normalized_category, 'name': name},
            'new': {'category': target_category, 'name': target_name},
        }
        result = {
            'status': 'renamed',
            'message': 'Skill package was renamed and is now active.',
        }
        result.update(payload)
        return tool_success('rename_skill', result)

    def remove_skill(self, name: str, category: Optional[str], reason: Optional[str] = None) -> Dict[str, Any]:
        """Remove an existing reusable skill package.

        Use this when a skill is superseded or no longer correct.

        Args:
            name: Skill name, or "category/name" when category is omitted.
            category: Skill category directory.
            reason: Why the skill should be removed.
        """
        lazyllm.LOG.info(f'[remove_skill] called name={name!r} category={category!r} reason={reason!r}')
        resolved = resolve_skill_editor_identity(name, category, 'remove_skill')
        if resolved.get('error'):
            return tool_error('remove_skill', resolved['error'])
        normalized_category = resolved['category']
        name = resolved['name']
        lazyllm.LOG.info(f'[remove_skill] lookup category={normalized_category!r} name={name!r}')

        try:
            remove_remote_skill(normalized_category, name, fs=self.remote_fs)
        except Exception as exc:
            return tool_error('remove_skill', f'Failed to remove skill package: {exc}')
        return tool_success('remove_skill', {
            'status': 'removed',
            'message': 'Skill was removed and is no longer active.',
        })
