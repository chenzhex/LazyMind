from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef

from .runtime_port import RuntimePort

CANDIDATE_STEPS = ('repair', 'abtest')
SECRET = re.compile(r'(api[_-]?key|token|secret|password|authorization|llm_config)', re.I)
AUTH_SECRET = re.compile(
    r'(?i)\bauthorization\s*[:=]\s*(?:bearer|basic|token)?\s*[^\s,;)\]}]+'
)
INLINE_SECRET = re.compile(
    r'(?i)\b(authorization|api[_-]?key|token|secret|password)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;)\]}]+'
)
FILE_URI = re.compile(r'file:///[^\n\r,;)\]}]+')
ABS_PATH = re.compile(r'(?<![:/\w])/(?!/)(?:[^\n\r,;)\]}\"\']*/)+[^\n\r,;)\]}\"\']*')


class ProjectionService:
    def __init__(self, root: Path, runtime: RuntimePort) -> None:
        self.root = root
        self.runtime = runtime
        self.download_root = root / 'downloads'
        self.download_root.mkdir(parents=True, exist_ok=True)

    def gates(self, thread_id: str) -> dict[str, Any]:
        self._require_thread(thread_id)
        store = self.runtime.store()
        try:
            effective = store.effective_artifacts(thread_id)
            items = []
            for step in C.STEPS:
                artifact_id = C.ROOTS[step]
                key = ArtifactKey.of(artifact_id)
                versions = sorted(record.ref.version for record in store.history(thread_id, key))
                effective_ref = effective.get(key)
                items.append({
                    'step': step,
                    'artifact_id': artifact_id,
                    'versions': versions,
                    'effective_version': None if effective_ref is None else effective_ref.version,
                    'latest_version': max(versions, default=None),
                })
            return {'thread_id': thread_id, 'gates': items}
        finally:
            store.close()

    def gate_content(self, thread_id: str, step: str, version: int) -> dict[str, Any]:
        value = public_gate_value(step, self._gate_value(thread_id, step, version))
        return {'thread_id': thread_id, 'step': step, 'version': version, 'content': value}

    def gate_download(self, thread_id: str, step: str, version: int, file_format: str) -> Path:
        if file_format != 'json':
            raise HTTPException(422, 'format must be json')
        value = public_gate_value(step, self._gate_value(thread_id, step, version))
        target = self.download_root / thread_id / f'{step}-v{version}.{file_format}'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        return target

    def events(self, thread_id: str, step: str, after: int, limit: int) -> dict[str, Any]:
        self._require_thread(thread_id)
        if step not in C.ROOTS:
            raise HTTPException(422, f'step must be one of: {", ".join(C.STEPS)}')
        ids = {spec.artifact_id for spec in C.OUTPUTS[step]}
        items, next_after = [], after
        store = self.runtime.store()
        try:
            for event in store.events_since(after, thread_id):
                next_after = event.seq
                refs = [ref for ref in event.refs if ref.key.artifact_id in ids]
                if not refs:
                    continue
                item = {
                    'seq': event.seq,
                    'event_id': f'artifact:{event.seq}',
                    'kind': event.kind,
                    'refs': [_ref_text(ref) for ref in refs],
                    'message': f'{step} artifact {event.kind}',
                    'payload': {'ref_count': len(refs)},
                }
                items.append(item)
                if step == 'repair':
                    for ref in refs:
                        record = store.get(ref)
                        value = record.value if record is not None and isinstance(record.value, Mapping) else {}
                        items.extend(_repair_events(event.seq, ref, value))
                if len(items) >= limit:
                    break
        finally:
            store.close()
        return {'thread_id': thread_id, 'step': step, 'items': items, 'next_after': next_after}

    def candidates(self, thread_id: str, status: str, page_size: int, page_token: str) -> dict[str, Any]:
        run_ids = [thread_id] if thread_id else self.runtime.run_ids()
        if thread_id:
            self._require_thread(thread_id)
        items = []
        for run_id in run_ids:
            items.extend(self._candidate_items(run_id))
        if status:
            items = [item for item in items if item.get('status') == status]
        items.sort(key=lambda item: item['candidate_id'])
        items = [item for item in items if not page_token or item['candidate_id'] > page_token]
        page = items[:page_size]
        next_token = page[-1]['candidate_id'] if len(page) == page_size else ''
        return {'items': page, 'next_page_token': next_token}

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        thread_id, step, version = _parse_candidate_id(candidate_id)
        detail = self.gate_content(thread_id, step, version)
        content = detail['content'] if isinstance(detail['content'], Mapping) else {}
        return _candidate_row(thread_id, step, version, content, detail=True)

    def _candidate_items(self, thread_id: str) -> list[dict[str, Any]]:
        rows = []
        store = self.runtime.store()
        try:
            for step in CANDIDATE_STEPS:
                key = ArtifactKey.of(C.ROOTS[step])
                for record in store.history(thread_id, key):
                    rows.append(
                        _candidate_row(thread_id, step, record.ref.version, public_gate_value(step, record.value))
                    )
        finally:
            store.close()
        return rows

    def _gate_value(self, thread_id: str, step: str, version: int) -> object:
        self._require_thread(thread_id)
        if step not in C.ROOTS:
            raise HTTPException(422, f'step must be one of: {", ".join(C.STEPS)}')
        if version < 1:
            raise HTTPException(422, 'version must be positive')
        ref = ArtifactRef(ArtifactKey.of(C.ROOTS[step]), version)
        store = self.runtime.store()
        try:
            record = store.get(ref)
            if record is None or record.run_id != thread_id:
                raise HTTPException(404, 'gate artifact version not found')
            return record.value
        finally:
            store.close()

    def _require_thread(self, thread_id: str) -> None:
        if self.runtime.run_config(thread_id) is None:
            raise HTTPException(404, f'thread not found: {thread_id}')


def public_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): '<redacted>' if SECRET.search(str(key)) else public_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public_value(item) for item in value]
    if isinstance(value, tuple):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        text = AUTH_SECRET.sub('authorization=<redacted>', value)
        text = INLINE_SECRET.sub(lambda match: f'{match.group(1)}=<redacted>', text)
        return ABS_PATH.sub('<redacted-path>', FILE_URI.sub('file://<redacted-path>', text))
    return value


def public_gate_value(step: str, value: object) -> object:
    data = public_value(value)
    if not isinstance(data, Mapping):
        return data
    if step == 'dataset':
        return _pick(data, ('id', 'dataset_id', 'size', 'case_ids', 'stats', 'checks', 'warnings',
                            'rows', 'cases', 'download_cases'))
    if step == 'eval':
        return _pick(data, ('id', 'total', 'case_ids', 'metrics', 'quality_counts', 'failure_type_counts',
                            'retrieval_failure_type_counts', 'execution_failures', 'checks', 'rows'))
    if step == 'analysis':
        result = _pick(data, ('id', 'case_ids', 'total', 'issue_category_counts', 'issue_type_counts',
                              'affected_block_counts', 'failure_mode_counts', 'trace_quality',
                              'top_failure_patterns', 'checks', 'repair_group_queue', 'rows'))
        if isinstance(result.get('rows'), list):
            result['rows'] = result['rows'][:50]
        return result
    if step == 'repair':
        result = _pick(data, ('status', 'message', 'diff', 'patch', 'content', 'files',
                              'winning_attempt', 'validation_summary'))
        result['files'] = [item for item in result.get('files', []) if _safe_file(str(item))]
        loop = data.get('repair_loop') if isinstance(data.get('repair_loop'), Mapping) else {}
        if loop:
            result['repair_summary'] = {
                'status': loop.get('status'),
                'message': loop.get('message'),
                'attempt_count': loop.get('attempt_count'),
                'best_attempt': loop.get('best_attempt'),
                'best_attempt_status': loop.get('best_attempt_status'),
                'selected_group': loop.get('selected_group') or {},
                'event_count': len(loop.get('events') or []),
            }
        return result
    if step == 'abtest':
        return _pick(data, ('id', 'status', 'verdict', 'case_ids', 'case_count', 'metrics',
                            'goodcase_guard', 'decision', 'reasons'))
    return data


def _candidate_row(thread_id: str, step: str, version: int, content: object, *, detail: bool = False) -> dict[str, Any]:
    data = content if isinstance(content, Mapping) else {}
    files = [str(item) for item in data.get('files') or [] if _safe_file(str(item))]
    row = {
        'candidate_id': f'{thread_id}:{C.ROOTS[step]}@v{version}',
        'thread_id': thread_id,
        'source_step': step,
        'source_ref': f'{C.ROOTS[step]}@v{version}',
        'status': str(data.get('status') or ''),
        'summary': _summary(data),
    }
    if detail:
        row['files'] = files
    return row


def _summary(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: public_value(data[key])
        for key in ('status', 'message', 'winning_attempt', 'verdict', 'decision', 'metrics')
        if key in data
    }


def _pick(data: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data}


def _repair_events(seq: int, ref: ArtifactRef, value: Mapping[str, Any]) -> list[dict[str, Any]]:
    artifact_id = ref.key.artifact_id
    if artifact_id == C.REPAIR_PLAN:
        return [_repair_event(seq, ref, 'plan', {
            'phase': 'repair_plan',
            'source': 'repair',
            'kind': C.REPAIR_PLAN,
            'status': str(value.get('status') or ''),
            'title': 'Repair plan',
            'summary': str(value.get('blocked_reason') or value.get('message') or value.get('status') or ''),
            'selected_group': value.get('selected_group') or {},
        })]
    if artifact_id == C.REPAIR_CANDIDATE_WORKSPACE:
        return [_repair_event(seq, ref, 'workspace', {
            'phase': 'repair_workspace',
            'source': 'repair',
            'kind': C.REPAIR_CANDIDATE_WORKSPACE,
            'status': str(value.get('status') or ''),
            'title': 'Candidate workspace',
            'summary': str(value.get('status') or ''),
            'workspace_kind': value.get('workspace_kind'),
        })]
    if artifact_id == C.REPAIR_LOOP_RESULT:
        events = [_repair_loop_event(seq, ref, value)]
        for attempt in value.get('attempts') or []:
            if isinstance(attempt, Mapping):
                events.extend(_repair_attempt_events(seq, ref, attempt))
        if not value.get('attempts') and isinstance(value.get('events'), list):
            events.extend(
                _repair_event(seq, ref, f'loop_event_{index}', event)
                for index, event in enumerate(value.get('events') or [])
                if isinstance(event, Mapping)
            )
        return events
    if artifact_id == C.REPAIR_VERIFIED_PATCH:
        files = [item for item in value.get('files', []) if _safe_file(str(item))]
        return [_repair_event(seq, ref, 'verified_patch', {
            'phase': 'repair_verified_patch',
            'source': 'repair',
            'kind': C.REPAIR_VERIFIED_PATCH,
            'status': str(value.get('status') or ''),
            'title': 'Verified patch',
            'summary': str(value.get('message') or value.get('status') or ''),
            'files': files,
            'winning_attempt': value.get('winning_attempt'),
            'validation_summary': value.get('validation_summary') or {},
        })]
    return []


def _repair_loop_event(seq: int, ref: ArtifactRef, value: Mapping[str, Any]) -> dict[str, Any]:
    return _repair_event(seq, ref, 'loop', {
        'phase': 'repair_loop',
        'source': 'repair',
        'kind': C.REPAIR_LOOP_RESULT,
        'status': str(value.get('status') or ''),
        'title': 'Repair loop',
        'summary': str(value.get('message') or value.get('status') or ''),
        'attempt_count': value.get('attempt_count') or len(value.get('attempts') or []),
        'best_attempt': value.get('best_attempt'),
        'best_attempt_status': value.get('best_attempt_status'),
    })


def _repair_attempt_events(seq: int, ref: ArtifactRef, attempt: Mapping[str, Any]) -> list[dict[str, Any]]:
    attempt_no = attempt.get('attempt')
    trace = attempt.get('opencode_trace') if isinstance(attempt.get('opencode_trace'), Mapping) else {}
    rows = [_repair_event(seq, ref, f'attempt_{attempt_no}', {
        'phase': 'repair_attempt',
        'source': 'repair',
        'kind': 'repair.attempt',
        'status': str(attempt.get('status') or ''),
        'title': f'Repair attempt {attempt_no}',
        'summary': str((attempt.get('patch_base_decision') or {}).get('reason') or attempt.get('status') or ''),
        'attempt': attempt_no,
        'files': [item for item in attempt.get('files_changed', []) if _safe_file(str(item))],
        'decision': attempt.get('patch_base_decision') or {},
        'detail': _pick(trace, ('provider', 'model', 'returncode', 'event_counts',
                                'duration_seconds', 'setup_seconds', 'first_response_seconds')),
    })]
    for index, event in enumerate(attempt.get('events') or []):
        if isinstance(event, Mapping):
            phase = str(event.get('phase') or '')
            rows.append(_repair_event(seq, ref, f'attempt_{attempt_no}_{index}', dict(event) | {
                'attempt': attempt_no,
                'detail': _repair_event_detail(phase, attempt),
            }))
    return rows


def _repair_event(seq: int, ref: ArtifactRef, suffix: object, payload: Mapping[str, Any]) -> dict[str, Any]:
    ref_text = _ref_text(ref)
    data = public_value(dict(payload) | {'artifact_id': ref.key.artifact_id, 'artifact_ref': ref_text})
    paths = [item for item in data.get('paths', data.get('files', [])) or [] if _safe_file(str(item))]
    if 'paths' in data:
        data['paths'] = paths
    if 'files' in data:
        data['files'] = paths
    kind = str(data.get('kind') or data.get('phase') or 'repair.event')
    summary = str(data.get('summary') or data.get('title') or kind)
    return {
        'seq': seq,
        'event_id': f'repair:{seq}:{suffix}',
        'kind': kind,
        'refs': [ref_text],
        'message': summary,
        'payload': data,
    }


def _repair_event_detail(phase: str, attempt: Mapping[str, Any]) -> dict[str, Any]:
    if phase == 'pre_validation':
        pre = attempt.get('pre_validation') if isinstance(attempt.get('pre_validation'), Mapping) else {}
        return _pick(pre, ('status', 'reason', 'checks', 'files'))
    if phase == 'candidate_eval':
        candidate = attempt.get('candidate_validation')
        candidate = candidate if isinstance(candidate, Mapping) else {}
        summary = candidate.get('candidate_eval_summary')
        comparison = candidate.get('comparison')
        service = candidate.get('service')
        return {
            'status': candidate.get('status'),
            'accepted': candidate.get('accepted'),
            'reason': candidate.get('reason'),
            'case_ids': candidate.get('case_ids') or [],
            'service': _pick(service, ('status', 'healthcheck')) if isinstance(service, Mapping) else {},
            'metrics': (summary or {}).get('metrics') if isinstance(summary, Mapping) else {},
            'comparison': _pick(comparison, ('status', 'verdict', 'metrics', 'goodcase_guard'))
            if isinstance(comparison, Mapping) else {},
        }
    if phase == 'candidate_analysis':
        delta = attempt.get('analysis_delta') if isinstance(attempt.get('analysis_delta'), Mapping) else {}
        return _pick(
            delta,
            ('status', 'target_group_status', 'recommended_action', 'metric_delta', 'new_group_count'),
        )
    if phase == 'decision':
        decision = attempt.get('patch_base_decision')
        return _pick(decision, ('action', 'reason')) if isinstance(decision, Mapping) else {}
    return {}


def _parse_candidate_id(candidate_id: str) -> tuple[str, str, int]:
    thread_id, _, raw = candidate_id.partition(':')
    artifact, _, raw_version = raw.partition('@v')
    step = next((name for name in CANDIDATE_STEPS if C.ROOTS[name] == artifact), '')
    if not thread_id or not step or not raw_version.isdigit():
        raise HTTPException(404, 'candidate not found')
    return thread_id, step, int(raw_version)


def _safe_file(value: str) -> bool:
    return bool(value and not value.startswith('/') and '..' not in Path(value).parts)


def _ref_text(ref: ArtifactRef) -> str:
    key = ref.key.artifact_id if not ref.key.partition else f'{ref.key.artifact_id}[{ref.key.partition}]'
    return f'{key}@v{ref.version}'


__all__ = ['ProjectionService', 'public_value']
