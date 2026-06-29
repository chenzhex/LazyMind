from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx

DISABLED_TOOLS = tuple(
    'temp_kb calculator wikipedia web_search academic_search url_fetch multimodal image_generator image_editor '
    'vocab_learn read_memory memory_editor skill_editor local_fs feishu notion'.split()
)
HEX_SESSION = re.compile(r'^[0-9a-fA-F]+$')


class _ChatDeadlineExceeded(TimeoutError):
    pass


def call_chat_answer(case: Mapping[str, Any], target_config: Mapping[str, Any], kb_id: str) -> dict[str, Any]:
    try:
        session_id = _session_id(target_config.get('session_id'))
        target = _target(target_config, kb_id, session_id)
        payload = _chat_payload(case, target_config, kb_id, session_id)
        deadline = time.monotonic() + _float(target_config.get('case_deadline_seconds'), 60.0)
        timeout = httpx.Timeout(
            connect=_float(target_config.get('connect_timeout_seconds'), 5.0),
            write=_float(target_config.get('write_timeout_seconds'), 60.0),
            read=None,
            pool=_float(target_config.get('pool_timeout_seconds'), 5.0),
        )
    except (TypeError, ValueError) as exc:
        target = {'target_chat_url': str(target_config.get('target_chat_url') or ''), 'kb_id': kb_id}
        return failed_rag_answer(case, {}, target, 'chat_config_error', str(exc))
    result: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    closer: dict[str, Any] = {}
    worker = threading.Thread(
        target=_chat_worker,
        args=(case, target, payload, timeout, deadline, result, closer),
        daemon=True,
    )
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        response = closer.get('response')
        if response is not None:
            response.close()
        return failed_rag_answer(case, {}, target, 'chat_timeout', 'chat stream exceeded case deadline')
    if not result.empty():
        return result.get()
    return failed_rag_answer(case, {}, target, 'chat_unknown_error', 'no result')


def _chat_worker(
    case: Mapping[str, Any],
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    timeout: httpx.Timeout,
    deadline: float,
    result: queue.Queue[dict[str, Any]],
    closer: dict[str, Any],
) -> None:
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream('POST', target['target_chat_url'], json=payload, headers={
                'Accept': 'text/event-stream',
                'Content-Type': 'application/json',
            }) as response:
                closer['response'] = response
                if response.status_code != 200:
                    result.put(failed_rag_answer(case, {}, target, 'chat_http_error', f'HTTP {response.status_code}'))
                    return
                try:
                    result.put(normalize_chat_answer(case, _consume_sse(response, deadline), target))
                finally:
                    response.close()
    except _ChatDeadlineExceeded:
        result.put(failed_rag_answer(case, {}, target, 'chat_timeout', 'chat stream exceeded case deadline'))
    except httpx.HTTPError as exc:
        result.put(failed_rag_answer(case, {}, target, 'chat_transport_error', str(exc)))
    except Exception as exc:
        result.put(failed_rag_answer(case, {}, target, 'chat_unknown_error', f'{type(exc).__name__}: {exc}'))


def normalize_chat_answer(
    case: Mapping[str, Any], stream: Mapping[str, Any], target: Mapping[str, Any],
) -> dict[str, Any]:
    frames = [item for item in stream.get('frames') or () if isinstance(item, Mapping)]
    error = next((_business_error(frame) for frame in frames if _business_error(frame)), None)
    protocol_error = stream.get('protocol_error')
    data_frames = [_data(frame) for frame in frames]
    answer = str(stream.get('answer') or _last_value(data_frames, 'message', 'answer', 'text', 'result')).strip()
    contexts, doc_ids, chunk_ids = _sources(data_frames)
    trace_id = str(_last_value(data_frames, 'trace_id') or target.get('trace_id') or '')
    if error:
        return failed_rag_answer(case, stream, target, error['type'], error['message'])
    if protocol_error:
        return failed_rag_answer(case, stream, target, 'chat_protocol_error', str(protocol_error))
    return _answer_base(case, stream, target) | {
        'answer': answer,
        'status': 'ok',
        'chat_error': None,
        'tool_errors': _tool_errors(data_frames),
        'contexts': contexts,
        'doc_ids': doc_ids,
        'chunk_ids': chunk_ids,
        'trace_id': trace_id,
        'evidence_status': 'found' if contexts or doc_ids or chunk_ids else 'empty',
    }


def failed_rag_answer(
    case: Mapping[str, Any],
    stream: Mapping[str, Any],
    target: Mapping[str, Any],
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return _answer_base(case, stream, target) | {
        'status': 'failed',
        'chat_error': {'type': error_type, 'message': message},
        'evidence_status': 'failed',
    }


def _answer_base(case: Mapping[str, Any], stream: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'case_id': str(case.get('id') or ''),
        'case': dict(case),
        'case_metadata': {'kb_id': target.get('kb_id', '')},
        'question': str(case.get('question') or ''),
        'answer': str(stream.get('answer') or ''),
        'tool_errors': [],
        'contexts': [],
        'doc_ids': [],
        'chunk_ids': [],
        'trace_id': str(target.get('trace_id') or ''),
        'target': dict(target),
    }


def _consume_sse(response: httpx.Response, deadline: float) -> dict[str, Any]:
    frames, answer_parts = [], []
    protocol_error = ''
    finished = False
    data_lines = []
    for line in response.iter_lines():
        if time.monotonic() > deadline:
            raise _ChatDeadlineExceeded
        text = str(line or '').strip()
        if not text:
            if data_lines:
                finished, protocol_error = _append_frame(frames, answer_parts, data_lines)
                data_lines = []
                if finished or protocol_error:
                    break
            continue
        if text.startswith(':') or text.startswith(('event:', 'id:', 'retry:')):
            continue
        if text.startswith('data:'):
            text = text[5:].strip()
        data_lines.append(text)
    if data_lines and not finished and not protocol_error:
        finished, protocol_error = _append_frame(frames, answer_parts, data_lines)
    return {'frames': frames, 'answer': ''.join(answer_parts), 'ended': finished or not protocol_error,
            'protocol_error': protocol_error}


def _append_frame(frames: list[dict[str, Any]], answer_parts: list[str], data_lines: list[str]) -> tuple[bool, str]:
    text = '\n'.join(data_lines).strip()
    if not text:
        return False, ''
    if text == '[DONE]':
        return True, ''
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return False, f'non-json SSE line: {text[:120]}'
    if not isinstance(frame, Mapping):
        return False, 'SSE line JSON is not an object'
    frames.append(dict(frame))
    data = _data(frame)
    answer_parts.append(str(data.get('delta') or data.get('text') or ''))
    return str(data.get('status') or '').upper() == 'FINISHED', ''


def _chat_payload(
    case: Mapping[str, Any], target_config: Mapping[str, Any], kb_id: str, session_id: str,
) -> dict[str, Any]:
    payload = {
        'query': str(case.get('question') or ''),
        'history': [],
        'session_id': session_id,
        'filters': {'kb_id': [kb_id]},
        'reasoning': False,
        'mode': 'manual',
        'has_subagents': False,
        'enable_plugin': False,
        'enable_subagent': False,
        'use_memory': False,
        'disabled_tools': list(DISABLED_TOOLS),
    }
    if target_config.get('algorithm_id'):
        payload['algorithm_id'] = str(target_config['algorithm_id'])
    if target_config.get('trace', True):
        payload['trace'] = True
    if isinstance(target_config.get('llm_config'), Mapping):
        payload['llm_config'] = dict(target_config['llm_config'])
    return payload


def _target(config: Mapping[str, Any], kb_id: str, session_id: str) -> dict[str, str]:
    target = {
        'target_chat_url': _chat_stream_url(str(config.get('target_chat_url') or '').strip()),
        'kb_id': kb_id,
    }
    for key in ('target_id', 'target_kind', 'target_label', 'algorithm_id'):
        if config.get(key):
            target[key] = str(config[key])
    if config.get('trace', True):
        target['trace_id'] = session_id
    if not target['target_chat_url']:
        raise ValueError('target_chat_url is required')
    if not kb_id:
        raise ValueError('kb_id is required')
    return target


def _data(frame: Mapping[str, Any]) -> Mapping[str, Any]:
    data = frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
    return data if isinstance(data, Mapping) else {}


def _business_error(frame: Mapping[str, Any]) -> dict[str, str] | None:
    data = _data(frame)
    code = data.get('code', frame.get('code'))
    status = str(data.get('status') or '').upper()
    if code not in (None, 200, '200') or status == 'FAILED':
        return {'type': 'chat_business_error',
                'message': str(data.get('msg') or frame.get('msg') or data.get('message') or status)}
    return None


def _last_value(items: list[Mapping[str, Any]], *keys: str) -> Any:
    return next((item[key] for item in reversed(items) for key in keys if item.get(key)), '')


def _sources(items: list[Mapping[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    sources = [src for item in items for src in item.get('sources') or [] if isinstance(src, Mapping)]
    contexts = [src.get('content') or src.get('text') or src.get('chunk') for src in sources]
    doc_ids = [src.get('doc_id') or src.get('docid') or src.get('document_id') for src in sources]
    chunk_ids = [src.get('chunk_id') or src.get('chunkid') or src.get('id') for src in sources]
    return _compact(contexts), _compact(doc_ids), _compact(chunk_ids)


def _tool_errors(items: list[Mapping[str, Any]]) -> list[Any]:
    return [item.get(key) for item in items for key in ('tool_error', 'tool_errors', 'kb_errors') if item.get(key)]


def _compact(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or '').strip()))


def _chat_stream_url(url: str) -> str:
    if not url:
        raise ValueError('target_chat_url is required')
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('target_chat_url must be an http(s) URL')
    return urlunparse((parsed.scheme, parsed.netloc, '/api/chat/stream', '', '', ''))


def _session_id(value: Any) -> str:
    text = str(value or uuid4().hex).strip().lower()
    if not HEX_SESSION.fullmatch(text):
        return uuid4().hex
    return text


def _float(value: Any, default: float) -> float:
    return float(default if value in (None, '') else value)
