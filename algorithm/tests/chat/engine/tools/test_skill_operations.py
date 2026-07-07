from __future__ import annotations

import pytest

from lazymind.chat.engine.tools.infra.skill_operations import (
    apply_skill_package_operations,
    normalize_skill_package_path,
)


def test_apply_skill_package_operations_updates_multiple_files():
    files = {
        'SKILL.md': (
            '---\n'
            'name: package-skill\n'
            'category: coding\n'
            'description: Package skill.\n'
            '---\n'
            '# Package Skill\n'
            'Old body\n'
        ),
        'references/old.md': 'old reference\n',
    }

    edited, payload = apply_skill_package_operations(files, [
        {
            'op': 'patch_file',
            'path': 'SKILL.md',
            'old_text': 'Old body',
            'new_text': 'New body',
        },
        {
            'op': 'write_file',
            'path': 'scripts/check.py',
            'content': 'print("ok")\n',
        },
        {
            'op': 'delete_file',
            'path': 'references/old.md',
        },
    ])

    assert 'New body' in edited['SKILL.md']
    assert edited['scripts/check.py'] == 'print("ok")\n'
    assert 'references/old.md' not in edited
    assert [op['op'] for op in payload] == ['patch_file', 'write_file', 'delete_file']


@pytest.mark.parametrize('path', ['/x', '../x', 'refs/../x', 'refs//x', r'refs\x'])
def test_normalize_skill_package_path_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        normalize_skill_package_path(path)


def test_apply_skill_package_operations_rejects_skill_md_delete():
    with pytest.raises(Exception, match='SKILL.md cannot be deleted'):
        apply_skill_package_operations({'SKILL.md': 'content'}, [
            {'op': 'delete_file', 'path': 'SKILL.md'},
        ])
