from __future__ import annotations

import asyncio
import json
import math
import os
import re
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
HEX = re.compile(r'^[0-9a-fA-F]+$')
DELTA_KEYS = ('delta', 'answer_delta', 'content_delta')
FINAL_ANSWER_KEYS = ('answer', 'message', 'text', 'result', 'content')
SOURCE_KEYS = ('sources', 'source_documents', 'retrieved_contexts', 'contexts', 'documents')
TOOL_RESULT = re.compile(r'<tool_result>(.*?)</tool_result>', re.S)
DEFAULT_CASE_DEADLINE_SECONDS = 300.0
DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS = 60.0


def call_chat_answer(case: Mapping[str, Any], target_config: Mapping[str, Any], kb_id: str) -> dict[str, Any]:
    try:
        kb_ids = list(dict.fromkeys(item.strip() for item in str(kb_id or '').split(';') if item.strip()))
        session_id = str(target_config.get('session_id') or uuid4().hex).strip().lower()
        session_id = session_id if HEX.fullmatch(session_id) else uuid4().hex
        target = {
            'target_chat_url': _stream_url(str(target_config.get('target_chat_url') or '')),
            'kb_id': ';'.join(kb_ids),
            'conversation_id': str(
                target_config.get('conversation_id') or session_id
            ).strip(),
            'user_id': str(target_config.get('user_id') or '0').strip() or '0',
        }
        target.update({
            key: str(target_config[key])
            for key in ('target_id', 'target_kind', 'target_label', 'algorithm_id')
            if target_config.get(key)
        })
        target['trace_id'] = session_id
        if not kb_ids:
            return failed_rag_answer(case, {}, target, 'dataset_contract_error',
                                     'case routing metadata missing kb_id')
        payload = {
            'message': {
                'query': str(case.get('question') or ''),
                'history': [],
                'current_turn_seq': 1,
            },
            'conversation': {
                'session_id': session_id,
                'conversation_id': target['conversation_id'],
                'user_id': target['user_id'],
                'mode': 'manual',
            },
            'retrieval': {'filters': {'kb_id': kb_ids}},
            'runtime': {
                'debug': False,
                'reasoning': False,
                'trace': True,
            },
            'personalization': {'use_memory': False},
            'agent': {
                'disabled_tools': list(DISABLED_TOOLS),
                'available_skills': [],
                'has_subagents': False,
                'enable_subagent': False,
            },
            'plugin': {'enable_plugin': False},
        }
        if target_config.get('algorithm_id'):
            payload['algorithm_id'] = str(target_config['algorithm_id'])
        if isinstance(target_config.get('llm_config'), Mapping):
            payload['runtime']['llm_config'] = dict(target_config['llm_config'])
        timeout = httpx.Timeout(
            connect=_number(target_config.get('connect_timeout_seconds'), 5.0),
            write=_number(target_config.get('write_timeout_seconds'), 60.0),
            read=None,
            pool=_number(target_config.get('pool_timeout_seconds'), 5.0),
        )
        deadline_seconds = _number(
            target_config.get('case_deadline_seconds') or os.getenv('LAZYMIND_EVO_CHAT_CASE_DEADLINE_SECONDS'),
            DEFAULT_CASE_DEADLINE_SECONDS,
        )
        first_frame_timeout = target_config.get('first_frame_timeout_seconds')
        if not first_frame_timeout:
            first_frame_timeout = os.getenv(
                'LAZYMIND_EVO_CHAT_FIRST_FRAME_TIMEOUT_SECONDS'
            )
        first_frame_timeout_seconds = _number(
            first_frame_timeout,
            DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS,
        )
        deadline = time.monotonic() + deadline_seconds
    except (TypeError, ValueError) as exc:
        target = {'target_chat_url': str(target_config.get('target_chat_url') or ''), 'kb_id': str(kb_id or '')}
        return failed_rag_answer(case, {}, target, 'chat_config_error', str(exc))
    try:
        return asyncio.run(asyncio.wait_for(
            _run_chat(case, target, payload, timeout, deadline, first_frame_timeout_seconds),
            timeout=deadline_seconds,
        ))
    except asyncio.TimeoutError:
        return failed_rag_answer(case, {}, target, 'chat_timeout',
                                 'chat stream exceeded case deadline after 0 frame(s)')
    except httpx.HTTPError as exc:
        return failed_rag_answer(case, {}, target, 'chat_transport_error', str(exc))
    except Exception as exc:
        return failed_rag_answer(case, {}, target, 'chat_unknown_error', f'{type(exc).__name__}: {exc}')


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


async def _run_chat(
    case: Mapping[str, Any],
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    timeout: httpx.Timeout,
    deadline: float,
    first_frame_timeout_seconds: float,
) -> dict[str, Any]:
    stream: dict[str, Any] = {'frames': [], 'answer': '', 'finished': False, 'natural_end': False}
    line_task: asyncio.Task[str] | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream('POST', target['target_chat_url'], json=payload, headers={
            'Accept': 'text/event-stream',
            'Content-Type': 'application/json',
        }) as response:
            if response.status_code != 200:
                return failed_rag_answer(case, stream, target, 'chat_http_error', f'HTTP {response.status_code}')
            routed = response.headers.get('X-Algorithm-Id')
            if routed:
                target = dict(target) | {'routed_algorithm_id': routed}
            routed_instance = response.headers.get('X-Instance-Host')
            if routed_instance:
                target = dict(target) | {'routed_instance_host': routed_instance}
            lines = response.aiter_lines()
            first_frame_deadline = time.monotonic() + first_frame_timeout_seconds
            try:
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if not stream['frames']:
                        remaining = min(remaining, first_frame_deadline - now)
                    if remaining <= 0:
                        await response.aclose()
                        if not stream['frames']:
                            return failed_rag_answer(
                                case,
                                stream,
                                target,
                                'chat_timeout',
                                'chat stream exceeded first-frame deadline after 0 frame(s)',
                            )
                        return failed_rag_answer(case, stream, target, 'chat_timeout',
                                                 _timeout_message(stream))
                    if line_task is None:
                        line_task = asyncio.create_task(anext(lines))
                    done, _ = await asyncio.wait({line_task}, timeout=min(1.0, remaining))
                    if not done:
                        continue
                    try:
                        line = line_task.result()
                    except StopAsyncIteration:
                        stream['natural_end'] = True
                        return _normalize(case, target, stream)
                    line_task = None
                    accepted = _accept_frame(case, target, stream, str(line or ''))
                    if accepted is not None:
                        return accepted
            finally:
                if line_task is not None and not line_task.done():
                    line_task.cancel()


def _accept_frame(
    case: Mapping[str, Any],
    target: Mapping[str, Any],
    stream: dict[str, Any],
    line: str,
) -> dict[str, Any] | None:
    text = str(line or '').strip()
    if not text or text.startswith(':') or text.startswith(('event:', 'id:', 'retry:')):
        return None
    if text.startswith('data:'):
        text = text[5:].strip()
    if text == '[DONE]':
        stream['finished'] = True
        return _normalize(case, target, stream)
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return failed_rag_answer(case, stream, target, 'chat_protocol_error', f'non-json SSE data: {text[:120]}')
    if not isinstance(frame, Mapping):
        return failed_rag_answer(case, stream, target, 'chat_protocol_error', 'SSE JSON is not an object')
    stream['frames'].append(dict(frame))
    data = frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
    codes = [value for value in (frame.get('code'), data.get('code')) if value is not None]
    status = str(data.get('status') or '').upper()
    if any(code not in (200, '200') for code in codes) or status == 'FAILED':
        message = data.get('msg') or frame.get('msg') or data.get('message') or status
        return failed_rag_answer(case, stream, target, 'chat_business_error', str(message))
    stream['answer'] += str(next((data[key] for key in (*DELTA_KEYS, 'text') if data.get(key)), ''))
    if status == 'FINISHED':
        stream['finished'] = True
        return _normalize(case, target, stream)
    return None


def _normalize(case: Mapping[str, Any], target: Mapping[str, Any], stream: Mapping[str, Any]) -> dict[str, Any]:
    data_frames = [
        frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
        for frame in stream.get('frames', [])
        if isinstance(frame, Mapping)
    ]
    answer = str(stream.get('answer') or _last(data_frames, FINAL_ANSWER_KEYS)).strip()
    if not (stream.get('finished') or stream.get('natural_end')):
        return failed_rag_answer(case, stream, target, 'chat_protocol_error', 'stream ended before FINISHED')
    if not answer:
        return failed_rag_answer(case, stream, target, 'chat_protocol_error', 'stream finished without answer text')
    contexts, doc_ids, chunk_ids, tool_errors, sources = [], [], [], [], []
    for data in data_frames:
        for key in ('tool_error', 'tool_errors', 'kb_errors'):
            if data.get(key):
                tool_errors.append(data[key])
        for key in SOURCE_KEYS:
            item = data.get(key)
            for source in item if isinstance(item, list) else [item] if item else []:
                if isinstance(source, Mapping):
                    sources.append(source)
    for match in TOOL_RESULT.finditer(answer):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        result = payload.get('result') if isinstance(payload, Mapping) else {}
        result = result.get('result') if isinstance(result, Mapping) else {}
        items = result.get('items') if isinstance(result, Mapping) else []
        if not isinstance(items, list):
            items = []
        sources.extend(item for item in items if isinstance(item, Mapping))
    target_kbs = [item for item in str(target.get('kb_id') or '').split(';') if item]
    fallback_kb = target_kbs[0] if len(target_kbs) == 1 else ''
    for source in sources:
        metadata = source.get('global_metadata')
        if not isinstance(metadata, Mapping):
            metadata = {}
        kb = str(source.get('kb_id') or metadata.get('kb_id') or metadata.get('dataset_id') or fallback_kb).strip()
        doc = str(source.get('doc_id') or source.get('docid') or source.get('document_id')
                  or metadata.get('docid') or metadata.get('core_document_id') or '').strip()
        chunk = str(source.get('chunk_id') or source.get('chunkid') or source.get('uid')
                    or source.get('id') or '').strip()
        doc_ref = doc if ':' in doc else f'{kb}:{doc}' if kb and doc else doc
        chunk_ref = chunk if ':' in chunk else f'{doc_ref}:{chunk}' if doc_ref and chunk else chunk
        contexts.append(source.get('content') or source.get('text') or source.get('chunk'))
        doc_ids.append(doc_ref)
        chunk_ids.append(chunk_ref)
    return _answer_base(case, stream, target) | {
        'answer': answer,
        'status': 'ok',
        'chat_error': None,
        'tool_errors': tool_errors,
        'contexts': _unique(contexts),
        'doc_ids': _unique(doc_ids),
        'chunk_ids': _unique(chunk_ids),
        'trace_id': str(_last(data_frames, ('trace_id',)) or target.get('trace_id') or ''),
        'evidence_status': 'found' if contexts or doc_ids or chunk_ids else 'empty',
    }


def _timeout_message(stream: Mapping[str, Any]) -> str:
    frames = [
        frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
        for frame in stream.get('frames', [])
        if isinstance(frame, Mapping)
    ]
    last = frames[-1] if frames else {}
    status = str(last.get('status') or '').strip()
    code = last.get('code')
    answer_len = len(str(stream.get('answer') or ''))
    parts = [f'chat stream exceeded case deadline after {len(frames)} frame(s)']
    if status:
        parts.append(f'last_status={status}')
    if code is not None:
        parts.append(f'last_code={code}')
    if answer_len:
        parts.append(f'answer_chars={answer_len}')
    return '; '.join(parts)


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


def _stream_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('target_chat_url must be an http(s) URL')
    return urlunparse((parsed.scheme, parsed.netloc, '/api/chat/stream', '', '', ''))


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or '').strip()))


def _last(items: list[Mapping[str, Any]], keys: tuple[str, ...]) -> Any:
    return next((item[key] for item in reversed(items) for key in keys if item.get(key)), '')


def _number(value: Any, default: float) -> float:
    result = float(default if value in (None, '') else value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError('timeout values must be positive finite numbers')
    return result
