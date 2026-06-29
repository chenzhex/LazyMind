import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .csv_loader import AUDIT_FIELDS, CASE_FIELDS, case_source, norm_text, normalize_eval_case


def assemble_dataset(
    cases: Mapping[str, Any] | Iterable[Mapping[str, Any]], *, min_case_count: int = 1,
) -> dict[str, Any]:
    rows = _rows(cases)
    id_counts = Counter(row['id'] for row in rows)
    question_counts = Counter(norm_text(row['question']) for row in rows)
    errors = [{'code': 'duplicate_id', 'id': case_id} for case_id, count in id_counts.items() if count > 1]
    errors += [{'code': 'duplicate_question', 'question': question} for question, count in question_counts.items()
               if question and count > 1]
    if len(rows) < min_case_count:
        errors.append({'code': 'too_few_cases', 'size': len(rows), 'min_case_count': min_case_count})
    return {
        'id': 'eval.dataset',
        'fields': list(CASE_FIELDS),
        'audit_fields': list(AUDIT_FIELDS),
        'download_fields': [*CASE_FIELDS, *AUDIT_FIELDS],
        'size': len(rows),
        'case_ids': [row['id'] for row in rows],
        'stats': {
            'question_type_counts': dict(Counter(row['question_type'] for row in rows)),
            'difficulty_counts': dict(Counter(row['difficulty'] for row in rows)),
        },
        'checks': {'ready': bool(rows) and not errors, 'errors': errors, 'warnings': _warnings(rows)},
        'case_provenance': [case_source(row) for row in rows],
        'cases': [{field: row.get(field, '') for field in CASE_FIELDS} for row in rows],
        'download_cases': [_download(row) for row in rows],
    }


def _rows(cases: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(cases, Mapping):
        return [normalize_eval_case(row, default_id=f'case_{index:04d}') for index, row in enumerate(cases, 1)]
    rows = []
    for case_id, row in sorted(cases.items()):
        if isinstance(row, Mapping) and row.get('id') and row.get('id') != case_id:
            raise ValueError(f'case partition mismatch: {case_id} != {row["id"]}')
        rows.append(normalize_eval_case(row, default_id=case_id))
    return rows


def _warnings(rows: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    warnings, seen = [], set()
    for row in rows:
        prep = row.get('source_preparation') if isinstance(row.get('source_preparation'), Mapping) else {}
        for item in prep.get('warnings', []):
            if not isinstance(item, Mapping):
                continue
            key = tuple(sorted((str(name), str(value)) for name, value in item.items()))
            if key in seen:
                continue
            seen.add(key)
            warnings.append({'code': str(item.get('code') or 'dataset_warning'),
                             'message': str(item.get('message') or ''),
                             'case_id': str(item.get('case_id') or ''),
                             'original_id': str(item.get('original_id') or ''),
                             'kb_id': str(item.get('kb_id') or '')})
    return warnings


def _download(row: Mapping[str, Any]) -> dict[str, Any]:
    audit = case_source(row)
    values = {field: row.get(field, '') for field in CASE_FIELDS}
    download = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, tuple)) else value
                for key, value in values.items()}
    download.update({field: audit[field] for field in AUDIT_FIELDS})
    return download
