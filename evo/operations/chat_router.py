from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlparse, urlunparse
from uuid import uuid4

import httpx
from tenacity import AsyncRetrying, retry_if_result, stop_after_attempt, wait_random_exponential

DEFAULT_DISABLED_TOOLS = tuple(
    'temp_kb calculator wikipedia web_search academic_search url_fetch multimodal image_generator image_editor '
    'vocab_learn read_memory memory_editor skill_editor local_fs feishu notion '
    'schedule create_schedule list_schedules cancel_schedule update_schedule trigger_schedule '
    'ask_user create_subagent list_subagents read_user_attachment find_user_attachment mcp plugin'.split()
)
EVO_EVAL_MEMORY = (
    'Evo evaluation request: use the knowledge-base tool as the evidence source, '
    'keep retrieval bounded, answer directly when enough evidence is available, '
    'and do not ask the user or schedule tasks.'
)
DELTA_KEYS = ('delta', 'answer_delta', 'content_delta', 'text')
EXPLICIT_ANSWER_KEYS = ('answer', 'message', 'result', 'content')
SOURCE_KEYS = ('sources', 'source_documents', 'retrieved_contexts', 'contexts', 'documents')
TOOL_TAG = re.compile(r'<(?P<tag>tool_call|tool_result)(?:\s[^>]*)?>(?P<body>.*?)</(?P=tag)>', re.S)
CONTROL_TAG = re.compile(r'<(?:tp|trp|tool_call|tool_result)(?:\s[^>]*)?>.*?</(?:tp|trp|tool_call|tool_result)>', re.S)
TRACE_ID = re.compile(r'^[0-9a-f]{32}$')
SSE_FIELD = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*:')
TOOL_DIAGNOSTIC_KEYS = ('tool_error', 'tool_errors', 'kb_errors', 'tool_result', 'tool_results')
DIAGNOSTIC_FRAME_KEY = '_evo_process_diagnostic'
PROCESS_DIAGNOSTIC_MARKERS = (
    'disabled_tools',
    'disabled tools',
    '[tool error]',
    'tool error',
    'tool execution failed',
    'tool disabled',
    'permission',
    'forbidden',
    'unauthorized',
    'not allowed',
    'scope',
)
MESSAGE_DIAGNOSTIC_MARKERS = (
    'disabled_tools',
    'disabled tools',
    '[tool error]',
    'tool execution failed',
    'tool disabled',
)
INVALID_FINAL_ANSWER_MARKERS = (
    '知识库检索服务暂时不可用',
    '知识库服务暂时不可用',
    '无法访问知识库',
    '无法检索知识库',
    '检索服务暂时不可用',
    'the knowledge base retrieval service is unavailable',
    'knowledge base retrieval service is unavailable',
    'kb retrieval service is unavailable',
    'retrieval service is unavailable',
)
INVALID_FINAL_ANSWER_MAX_CHARS = 180
INVALID_FINAL_ANSWER_APOLOGY = re.compile(r'^(?:抱歉|很抱歉|对不起|sorry)[，,。.!:\s]*')
PROCESS_DIAGNOSTIC_FIELDS = (
    'msg',
    'error',
    'exception',
    'traceback',
    'detail',
    'reason',
    'name',
    'tool_name',
)
STRUCTURED_SCAN_MAX_ITEMS = 20_000
STREAM_CLOSE_TIMEOUT_SECONDS = 0.1

DEFAULT_CASE_DEADLINE_SECONDS = 300.0
DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_WAIT_MAX_SECONDS = 2.0
ROUTER_ADMIN_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class RouterChatRequest:
    router_chat_url: str
    router_admin_url: str
    algorithm_id: str
    query: str
    kb_ids: tuple[str, ...]
    trace_id: str = ''
    conversation_id: str = ''
    user_id: str = '0'
    history: tuple[Mapping[str, Any], ...] = ()
    llm_config: Mapping[str, Any] | None = None
    disabled_tools: tuple[str, ...] = DEFAULT_DISABLED_TOOLS
    connect_timeout_seconds: float = 5.0
    write_timeout_seconds: float = 60.0
    pool_timeout_seconds: float = 5.0
    case_deadline_seconds: float = DEFAULT_CASE_DEADLINE_SECONDS
    first_frame_timeout_seconds: float = DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_wait_max_seconds: float = DEFAULT_RETRY_WAIT_MAX_SECONDS


def call_router_chat(request: RouterChatRequest) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _failed(
            {},
            _target_from_raw(request),
            'chat_runtime_error',
            'call async_call_router_chat from a running event loop',
        )

    return asyncio.run(async_call_router_chat(request))


async def async_call_router_chat(request: RouterChatRequest) -> dict[str, Any]:
    try:
        normalized = _normalize_request(request)
    except (TypeError, ValueError) as exc:
        return _failed({}, _target_from_raw(request), 'chat_config_error', str(exc))

    attempts = 0
    deadline = time.monotonic() + normalized.case_deadline_seconds

    async def attempt_once() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failed({}, _target(normalized), 'chat_timeout', 'chat retry budget exhausted before next attempt')
        attempt_request = normalized if attempts == 1 else _retry_request(normalized)
        attempt_request = replace(
            attempt_request,
            case_deadline_seconds=remaining,
            first_frame_timeout_seconds=min(attempt_request.first_frame_timeout_seconds, remaining),
        )
        return await _call_router_chat_once(attempt_request)

    wait_strategy = wait_random_exponential(multiplier=0.25, max=normalized.retry_wait_max_seconds)

    def wait_with_deadline(state: Any) -> float:
        return min(float(wait_strategy(state)), max(0.0, deadline - time.monotonic()))

    retryer = AsyncRetrying(
        stop=stop_after_attempt(normalized.max_attempts),
        wait=wait_with_deadline,
        retry=retry_if_result(_retryable_chat_result),
        retry_error_callback=_retry_exhausted_result,
        reraise=False,
    )
    result = await retryer(attempt_once)
    if attempts > 1:
        result = dict(result) | {'chat_attempt_count': attempts}
    return result


async def _call_router_chat_once(request: RouterChatRequest) -> dict[str, Any]:
    deadline = time.monotonic() + request.case_deadline_seconds
    timeout = httpx.Timeout(
        connect=request.connect_timeout_seconds,
        write=request.write_timeout_seconds,
        read=None,
        pool=request.pool_timeout_seconds,
    )

    try:
        return await _call_router_chat(request, timeout, deadline)
    except asyncio.TimeoutError:
        return _failed(
            {},
            _target(request),
            'chat_timeout',
            'chat stream exceeded case deadline after 0 frame(s)',
        )
    except httpx.HTTPError as exc:
        return _failed({}, _target(request), 'chat_transport_error', str(exc))
    except Exception as exc:
        return _failed({}, _target(request), 'chat_unknown_error', f'{type(exc).__name__}: {exc}')


async def _call_router_chat(
    request: RouterChatRequest,
    timeout: httpx.Timeout,
    deadline: float,
) -> dict[str, Any]:
    target = _target(request)
    payload = _payload(request)
    stream: dict[str, Any] = {
        'frames': [],
        'answer': '',
        'finished': False,
    }
    pending_data_lines: list[str] = []
    line_task: asyncio.Task[str] | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        algorithm_target = await _verify_algorithm(client, request, target, deadline)
        if isinstance(algorithm_target.get('chat_error'), Mapping):
            return algorithm_target
        target = algorithm_target
        first_frame_deadline = time.monotonic() + request.first_frame_timeout_seconds
        stream_cm = client.stream(
            'POST',
            request.router_chat_url,
            json=payload,
            headers={'Accept': 'text/event-stream', 'Content-Type': 'application/json'},
        )
        try:
            response = await asyncio.wait_for(
                stream_cm.__aenter__(),
                timeout=_remaining_seconds(deadline, first_frame_deadline, False),
            )
        except asyncio.TimeoutError:
            return _timeout_failure(stream, target)
        try:
            if response.status_code != 200:
                return _failed(stream, target, 'chat_http_error', await _http_error(response, deadline))

            routed = response.headers.get('X-Algorithm-Id', '').strip()
            if not routed:
                return _failed(stream, target, 'router_header_missing', 'missing X-Algorithm-Id response header')
            target = target | {'routed_algorithm_id': routed}
            if routed != request.algorithm_id:
                return _failed(
                    stream,
                    target,
                    'router_algorithm_mismatch',
                    f'expected {request.algorithm_id}, got {routed}',
                )

            routed_instance = response.headers.get('X-Instance-Host', '').strip()
            if routed_instance:
                target = target | {'routed_instance_host': routed_instance}

            lines = response.aiter_lines()
            try:
                while True:
                    remaining = _remaining_seconds(deadline, first_frame_deadline, bool(stream['frames']))
                    if remaining <= 0:
                        await _cancel_line_task(line_task)
                        line_task = None
                        await _close_response(response)
                        return _timeout_failure(stream, target)
                    if line_task is None:
                        line_task = asyncio.create_task(lines.__anext__())
                    done, _ = await asyncio.wait({line_task}, timeout=min(1.0, remaining))
                    if not done:
                        continue
                    try:
                        line = line_task.result()
                    except StopAsyncIteration:
                        if pending_data_lines:
                            accepted = _accept_payload(target, stream, '\n'.join(pending_data_lines))
                            if accepted is not None:
                                return accepted
                        return _normalize(target, stream)
                    line_task = None
                    payload_text = _sse_payload(str(line or ''), pending_data_lines)
                    if payload_text is None:
                        continue
                    accepted = _accept_payload(target, stream, payload_text)
                    if accepted is not None:
                        return accepted
            finally:
                await _cancel_line_task(line_task)
        finally:
            await _exit_stream(stream_cm)


async def _cancel_line_task(line_task: asyncio.Task[str] | None) -> None:
    if line_task is not None and not line_task.done():
        line_task.cancel()
        done, _ = await asyncio.wait({line_task}, timeout=STREAM_CLOSE_TIMEOUT_SECONDS)
        if done:
            await asyncio.gather(line_task, return_exceptions=True)
        else:
            line_task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _close_response(response: Any) -> None:
    try:
        await asyncio.wait_for(response.aclose(), timeout=STREAM_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass


async def _exit_stream(stream_cm: Any) -> None:
    try:
        await asyncio.wait_for(stream_cm.__aexit__(None, None, None), timeout=STREAM_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass


def _normalize_request(request: RouterChatRequest) -> RouterChatRequest:
    router_chat_url = _stream_url(request.router_chat_url)
    router_admin_url = _base_url(request.router_admin_url, 'router_admin_url')
    algorithm_id = str(request.algorithm_id or '').strip()
    if not algorithm_id:
        raise ValueError('algorithm_id is required')
    query = str(request.query or '').strip()
    if not query:
        raise ValueError('query is required')
    kb_ids = tuple(dict.fromkeys(str(item).strip() for item in request.kb_ids if str(item).strip()))
    if not kb_ids:
        raise ValueError('kb_ids is required')
    disabled_tools = tuple(dict.fromkeys((
        *DEFAULT_DISABLED_TOOLS,
        *(str(item).strip() for item in (request.disabled_tools or ()) if str(item).strip()),
    )))
    trace_id = str(request.trace_id or uuid4().hex).strip().lower()
    if not TRACE_ID.fullmatch(trace_id):
        raise ValueError('trace_id must be a 32-character lowercase hex string')
    conversation_id = str(request.conversation_id or trace_id).strip()
    user_id = str(request.user_id or '0').strip() or '0'
    history = tuple(dict(item) for item in request.history)
    return replace(
        request,
        router_chat_url=router_chat_url,
        router_admin_url=router_admin_url,
        algorithm_id=algorithm_id,
        query=query,
        kb_ids=kb_ids,
        trace_id=trace_id,
        conversation_id=conversation_id,
        user_id=user_id,
        history=history,
        disabled_tools=disabled_tools,
        connect_timeout_seconds=_positive_number(request.connect_timeout_seconds, 'connect_timeout_seconds'),
        write_timeout_seconds=_positive_number(request.write_timeout_seconds, 'write_timeout_seconds'),
        pool_timeout_seconds=_positive_number(request.pool_timeout_seconds, 'pool_timeout_seconds'),
        case_deadline_seconds=_positive_number(request.case_deadline_seconds, 'case_deadline_seconds'),
        first_frame_timeout_seconds=_positive_number(
            request.first_frame_timeout_seconds,
            'first_frame_timeout_seconds',
        ),
        max_attempts=_int_between(request.max_attempts, 'max_attempts', 1, 5),
        retry_wait_max_seconds=_non_negative_number(request.retry_wait_max_seconds, 'retry_wait_max_seconds'),
    )


def _payload(request: RouterChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'algorithm_id': request.algorithm_id,
        'message': {
            'query': request.query,
            'history': list(request.history),
            'current_turn_seq': 1,
        },
        'conversation': {
            'session_id': request.trace_id,
            'conversation_id': request.conversation_id,
            'user_id': request.user_id,
            'mode': 'auto',
        },
        'retrieval': {'filters': {'kb_id': list(request.kb_ids)}},
        'runtime': {'debug': False, 'reasoning': True, 'trace': True},
        'personalization': {'use_memory': True, 'memory': EVO_EVAL_MEMORY},
        'agent': {
            'disabled_tools': list(request.disabled_tools),
            'available_skills': [],
            'has_subagents': False,
            'enable_subagent': False,
        },
        'plugin': {'enable_plugin': False},
    }
    if request.llm_config:
        payload['runtime']['llm_config'] = dict(request.llm_config)
    return payload


def _sse_payload(line: str, pending_data_lines: list[str]) -> str | None:
    text = line.rstrip('\r')
    if not text:
        if not pending_data_lines:
            return None
        payload = '\n'.join(pending_data_lines)
        pending_data_lines.clear()
        return payload
    if text.startswith(':'):
        return None
    if text.startswith('data:'):
        value = text[5:]
        pending_data_lines.append(value[1:] if value.startswith(' ') else value)
        return None
    if SSE_FIELD.match(text):
        return None
    return text


def _accept_payload(
    target: Mapping[str, Any],
    stream: dict[str, Any],
    payload_text: str,
) -> dict[str, Any] | None:
    text = payload_text.strip()
    if text == '[DONE]':
        stream['finished'] = True
        return _normalize(target, stream)
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return _failed(dict(stream) | {'answer': ''}, target, 'chat_protocol_error',
                       'non-json SSE data')
    if not isinstance(frame, Mapping):
        return _failed(dict(stream) | {'answer': ''}, target, 'chat_protocol_error',
                       'SSE JSON is not an object')

    data = frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
    tool_diagnostic = _is_tool_diagnostic(data) or _is_process_diagnostic(frame)
    stored_frame = dict(frame)
    if tool_diagnostic:
        stored_frame[DIAGNOSTIC_FRAME_KEY] = True
    stream['frames'].append(stored_frame)
    codes = [value for value in (frame.get('code'), data.get('code')) if value is not None]
    status = str(data.get('status') or '').upper()
    if (any(_code_failed(code) for code in codes) or status == 'FAILED') and not tool_diagnostic:
        message = data.get('msg') or frame.get('msg') or data.get('message') or status
        return _failed(dict(stream) | {'answer': ''}, target, 'chat_business_error', str(message))
    delta = _delta_text(data)
    if not tool_diagnostic:
        explicit_answer = _last([data], EXPLICIT_ANSWER_KEYS)
        if explicit_answer:
            stream['explicit_answer'] = str(explicit_answer)
        if delta:
            stream['answer'] += delta
    else:
        stream['answer'] = ''
        stream.pop('explicit_answer', None)
    if status == 'FINISHED':
        stream['finished'] = True
        return _normalize(target, stream)
    return None


def _normalize(target: Mapping[str, Any], stream: Mapping[str, Any]) -> dict[str, Any]:
    frames = [
        frame
        for frame in stream.get('frames', [])
        if isinstance(frame, Mapping)
    ]
    answer_frames = [_frame_data(frame) for frame in _answer_candidate_frames(frames)]
    raw_answer = str(
        stream.get('explicit_answer')
        or _last(answer_frames, EXPLICIT_ANSWER_KEYS)
        or stream.get('answer')
        or _last(answer_frames, ('text',))
    ).strip()
    answer = _answer_text(raw_answer)
    if not stream.get('finished'):
        return _failed(
            dict(stream) | {'answer': ''},
            target,
            'chat_protocol_error',
            'stream ended before FINISHED',
        )
    sources = _sources(answer_frames)
    contexts, doc_ids, chunk_ids = _source_refs(sources, target)
    if not answer:
        return _failed(dict(stream) | {'answer': ''}, target, 'chat_no_answer',
                       'stream finished without final answer text')
    if _invalid_final_answer(answer):
        return _failed(dict(stream) | {'answer': ''}, target, 'chat_invalid_answer',
                       'stream finished with retrieval-unavailable final answer')
    return {
        'status': 'ok',
        'answer': answer,
        'trace_id': str(target.get('trace_id') or ''),
        'algorithm_id': str(target.get('algorithm_id') or ''),
        'routed_algorithm_id': str(target.get('routed_algorithm_id') or ''),
        'routed_instance_host': str(target.get('routed_instance_host') or ''),
        'contexts': _unique(contexts),
        'doc_ids': _unique(doc_ids),
        'chunk_ids': _unique(chunk_ids),
        'sources': sources,
        'tool_errors': [],
        'frames': [],
        'chat_error': None,
        'target': dict(target),
    }


def _answer_candidate_frames(frames: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    last_tool_index = -1
    for index, frame in enumerate(frames):
        if _is_diagnostic_frame(frame):
            last_tool_index = index
    return frames[last_tool_index + 1:] if last_tool_index >= 0 else frames


def _failed(
    stream: Mapping[str, Any],
    target: Mapping[str, Any],
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return {
        'status': 'failed',
        'answer': str(stream.get('answer') or ''),
        'trace_id': str(target.get('trace_id') or ''),
        'algorithm_id': str(target.get('algorithm_id') or ''),
        'routed_algorithm_id': str(target.get('routed_algorithm_id') or ''),
        'routed_instance_host': str(target.get('routed_instance_host') or ''),
        'contexts': [],
        'doc_ids': [],
        'chunk_ids': [],
        'sources': [],
        'tool_errors': [],
        'frames': [],
        'chat_error': {'type': error_type, 'message': message},
        'target': dict(target),
    }


def _retry_request(request: RouterChatRequest) -> RouterChatRequest:
    trace_id = uuid4().hex
    return replace(request, trace_id=trace_id, conversation_id=trace_id)


def _retryable_chat_result(result: Mapping[str, Any]) -> bool:
    chat_error = result.get('chat_error') if isinstance(result.get('chat_error'), Mapping) else {}
    error_type = str(chat_error.get('type') or '')
    if error_type in {'chat_no_answer', 'chat_invalid_answer'}:
        return True
    return False


def _retry_exhausted_result(state: Any) -> dict[str, Any]:
    result = state.outcome.result()
    if not _retryable_chat_result(result):
        return dict(result)
    target = result.get('target') if isinstance(result.get('target'), Mapping) else {}
    chat_error = result.get('chat_error') if isinstance(result.get('chat_error'), Mapping) else {}
    exhausted_type = str(chat_error.get('type') or '').strip() + '_retry_exhausted'
    message = {
        'chat_invalid_answer_retry_exhausted': 'chat stream repeatedly returned retrieval-unavailable final answer',
        'chat_no_answer_retry_exhausted': 'chat stream repeatedly finished without final answer text',
    }.get(exhausted_type, 'chat stream repeatedly returned retryable failure')
    empty_result = dict(result) | {'answer': ''}
    failed = _failed(empty_result, target, exhausted_type, message)
    return failed | {'retry_exhausted': True}


def _code_failed(value: Any) -> bool:
    return str(value).strip() != '200'


def _delta_text(data: Mapping[str, Any]) -> str:
    return str(next((data[key] for key in DELTA_KEYS if data.get(key)), ''))


def _is_tool_diagnostic(data: Mapping[str, Any]) -> bool:
    name = str(data.get('name') or data.get('tool_name') or data.get('tool') or '').lower()
    has_tool_payload = any(data.get(key) for key in TOOL_DIAGNOSTIC_KEYS)
    has_tool_tag_text = any(TOOL_TAG.search(str(data.get(key) or '')) for key in DELTA_KEYS)
    has_tool_identity = bool(
        name
        or data.get('tool_call_id')
        or data.get('tool_call')
        or has_tool_payload
        or has_tool_tag_text
    )
    if not has_tool_identity:
        return False
    if has_tool_tag_text or has_tool_payload or data.get('error') or data.get('exception') or data.get('traceback'):
        return True
    return 'tool' in name or name.startswith('kb_') or '_kb_' in name


def _is_process_diagnostic(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return _has_process_marker(value)
    data = _frame_data(value)
    fields = [
        source[key]
        for source in (value, data)
        for key in PROCESS_DIAGNOSTIC_FIELDS
        if source.get(key)
    ]
    if _has_process_marker(fields):
        return True
    codes = [item for item in (value.get('code'), data.get('code')) if item is not None]
    status = str(value.get('status') or data.get('status') or '').upper()
    messages = [source['message'] for source in (value, data) if source.get('message')]
    if _has_process_marker(messages, MESSAGE_DIAGNOSTIC_MARKERS) or any(
        _is_permission_denied_message(message) for message in messages
    ):
        return True
    failed = any(_code_failed(code) for code in codes) or status == 'FAILED'
    return failed and _has_process_marker([*messages, _delta_text(data)])


def _has_process_marker(value: Any, markers: Sequence[str] = PROCESS_DIAGNOSTIC_MARKERS) -> bool:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        text = str(value or '').lower()
    return any(marker in text for marker in markers)


def _is_permission_denied_message(value: Any) -> bool:
    text = str(value or '').strip().lower().rstrip('.!')
    return text == 'permission denied' or text.startswith(('permission denied by ', 'permission denied:'))


def _frame_data(frame: Mapping[str, Any]) -> Mapping[str, Any]:
    data = frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
    return data if isinstance(data, Mapping) else {}


def _is_diagnostic_frame(frame: Mapping[str, Any]) -> bool:
    return (
        bool(frame.get(DIAGNOSTIC_FRAME_KEY))
        or _is_tool_diagnostic(_frame_data(frame))
        or _is_process_diagnostic(frame)
    )


def _target(request: RouterChatRequest) -> dict[str, Any]:
    return {
        'router_chat_url': request.router_chat_url,
        'router_admin_url': request.router_admin_url,
        'algorithm_id': request.algorithm_id,
        'kb_id': ';'.join(request.kb_ids),
        'trace_id': request.trace_id,
        'conversation_id': request.conversation_id,
        'user_id': request.user_id,
    }


async def _verify_algorithm(
    client: httpx.AsyncClient,
    request: RouterChatRequest,
    target: Mapping[str, Any],
    deadline: float,
) -> dict[str, Any]:
    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        return _failed({}, target, 'router_algorithm_timeout', 'algorithm detail request exceeded case deadline')
    timeout = min(ROUTER_ADMIN_TIMEOUT_SECONDS, remaining)
    try:
        response = await asyncio.wait_for(
            client.get(_algorithm_url(request.router_admin_url, request.algorithm_id)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        message = f'algorithm detail request exceeded {timeout:g}s'
        return _failed({}, target, 'router_algorithm_timeout', message)
    except httpx.HTTPError as exc:
        return _failed({}, target, 'router_algorithm_transport_error', str(exc))
    if response.status_code != 200:
        body = response.text.strip()
        suffix = f': {body[:300]}' if body else ''
        return _failed({}, target, 'router_algorithm_unavailable', f'HTTP {response.status_code}{suffix}')
    try:
        detail = response.json()
    except json.JSONDecodeError:
        return _failed({}, target, 'router_algorithm_protocol_error', 'algorithm detail is not JSON')
    if not isinstance(detail, Mapping):
        return _failed({}, target, 'router_algorithm_protocol_error', 'algorithm detail is not an object')
    instances = detail.get('instances') if isinstance(detail.get('instances'), list) else []
    healthy = [item for item in instances if isinstance(item, Mapping) and item.get('status') == 'healthy']
    if detail.get('status') != 'active' or not healthy:
        message = f'algorithm {request.algorithm_id} is not active with healthy instances'
        return _failed({}, target, 'router_algorithm_unhealthy', message)
    return dict(target) | {
        'algorithm_status': str(detail.get('status') or ''),
        'healthy_instances': len(healthy),
    }


def _target_from_raw(request: RouterChatRequest) -> dict[str, Any]:
    try:
        kb_id = ';'.join(str(item) for item in request.kb_ids)
    except TypeError:
        kb_id = str(request.kb_ids or '')
    return {
        'router_chat_url': str(request.router_chat_url or ''),
        'router_admin_url': str(request.router_admin_url or ''),
        'algorithm_id': str(request.algorithm_id or ''),
        'kb_id': kb_id,
        'trace_id': str(request.trace_id or ''),
        'conversation_id': str(request.conversation_id or ''),
        'user_id': str(request.user_id or ''),
    }


def _sources(frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for data in frames:
        for key in SOURCE_KEYS:
            _collect_sources(data.get(key), sources)
    return [dict(source) for source in sources]


def _collect_sources(value: Any, sources: list[dict[str, Any]]) -> None:
    stack: list[Any] = [value]
    scanned = 0
    while stack and scanned < STRUCTURED_SCAN_MAX_ITEMS:
        current = stack.pop()
        scanned += 1
        if isinstance(current, Mapping):
            if _source_like(current):
                sources.append(dict(current))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def _source_like(value: Mapping[str, Any]) -> bool:
    metadata = value.get('global_metadata')
    metadata = metadata if isinstance(metadata, Mapping) else {}
    has_text = any(value.get(key) for key in ('content', 'text', 'chunk'))
    has_ref = any(
        value.get(key) or metadata.get(key)
        for key in (
            'doc_id',
            'docid',
            'document_id',
            'core_document_id',
            'chunk_id',
            'chunkid',
            'segment_id',
            'segement_id',
            'uid',
            'id',
        )
    )
    return bool(has_text and has_ref)


def _answer_text(raw_answer: str) -> str:
    text = str(raw_answer or '').strip()
    if not text:
        return ''
    controls = list(CONTROL_TAG.finditer(text))
    cleaned = text[controls[-1].end():].strip() if controls else text
    cleaned = CONTROL_TAG.sub('', cleaned)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


def _invalid_final_answer(answer: str) -> bool:
    text = re.sub(r'\s+', ' ', str(answer or '').strip()).lower()
    if len(text) > INVALID_FINAL_ANSWER_MAX_CHARS:
        return False
    if not _has_process_marker(text, INVALID_FINAL_ANSWER_MARKERS):
        return False
    text = text.lstrip(' "\'“”‘’')
    if text.startswith(INVALID_FINAL_ANSWER_MARKERS):
        return True
    if match := INVALID_FINAL_ANSWER_APOLOGY.match(text):
        return text[match.end():].lstrip(' "\'“”‘’').startswith(INVALID_FINAL_ANSWER_MARKERS)
    return False


def _source_refs(
    sources: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
) -> tuple[list[Any], list[str], list[str]]:
    contexts: list[Any] = []
    doc_ids: list[str] = []
    chunk_ids: list[str] = []
    target_kbs = [item for item in str(target.get('kb_id') or '').split(';') if item]
    fallback_kb = target_kbs[0] if len(target_kbs) == 1 else ''
    for source in sources:
        metadata = source.get('global_metadata')
        metadata = metadata if isinstance(metadata, Mapping) else {}
        kb_id = str(
            source.get('kb_id')
            or source.get('dataset_id')
            or metadata.get('kb_id')
            or metadata.get('dataset_id')
            or fallback_kb
        ).strip()
        doc = str(
            source.get('doc_id')
            or source.get('docid')
            or source.get('document_id')
            or metadata.get('docid')
            or metadata.get('core_document_id')
            or ''
        ).strip()
        chunk = str(
            source.get('chunk_id')
            or source.get('chunkid')
            or source.get('segment_id')
            or source.get('segement_id')
            or source.get('uid')
            or source.get('id')
            or ''
        ).strip()
        doc_ref = doc if ':' in doc else f'{kb_id}:{doc}' if kb_id and doc else doc
        chunk_ref = chunk if ':' in chunk else f'{doc_ref}:{chunk}' if doc_ref and chunk else chunk
        contexts.append(source.get('content') or source.get('text') or source.get('chunk'))
        doc_ids.append(doc_ref)
        chunk_ids.append(chunk_ref)
    return contexts, doc_ids, chunk_ids


async def _http_error(response: httpx.Response, deadline: float) -> str:
    chunks: list[bytes] = []
    total = 0

    async def read_limited() -> None:
        nonlocal total
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            remaining = 512 - total
            if remaining <= 0:
                return
            chunks.append(chunk[:remaining])
            total += min(len(chunk), remaining)
            if total >= 512:
                return

    timeout = max(0.001, deadline - time.monotonic())
    try:
        await asyncio.wait_for(read_limited(), timeout=timeout)
    except asyncio.TimeoutError:
        return f'HTTP {response.status_code}: <error body read timed out>'
    body = b''.join(chunks).decode(errors='replace').strip()
    suffix = f': {body[:300]}' if body else ''
    return f'HTTP {response.status_code}{suffix}'


def _timeout_failure(stream: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    frames = [
        _frame_data(frame)
        for frame in stream.get('frames', [])
        if isinstance(frame, Mapping)
    ]
    if not frames:
        return _failed(stream, target, 'chat_timeout', 'chat stream exceeded first-frame deadline after 0 frame(s)')
    last = frames[-1]
    parts = [f'chat stream exceeded case deadline after {len(frames)} frame(s)']
    if last.get('status'):
        parts.append(f'last_status={last["status"]}')
    if last.get('code') is not None:
        parts.append(f'last_code={last["code"]}')
    return _failed(dict(stream) | {'answer': ''}, target, 'chat_timeout', '; '.join(parts))


def _remaining_seconds(deadline: float, first_frame_deadline: float, has_frames: bool) -> float:
    now = time.monotonic()
    remaining = deadline - now
    if not has_frames:
        remaining = min(remaining, first_frame_deadline - now)
    return remaining


def _stream_url(url: str) -> str:
    parsed = _parsed_http_url(url, 'router_chat_url')
    _ensure_path(parsed.path, 'router_chat_url', {'', '/', '/api/chat/stream'})
    return urlunparse((parsed.scheme, parsed.netloc, '/api/chat/stream', '', '', ''))


def _base_url(url: str, field: str) -> str:
    parsed = _parsed_http_url(url, field)
    _ensure_path(parsed.path, field, {'', '/'})
    return urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))


def _algorithm_url(router_admin_url: str, algorithm_id: str) -> str:
    parsed = urlparse(router_admin_url)
    path = f'/inner/algorithm/{quote(algorithm_id, safe="")}'
    return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))


def _parsed_http_url(url: str, field: str):
    parsed = urlparse(str(url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError(f'{field} must be an http(s) URL')
    return parsed


def _ensure_path(path: str, field: str, allowed: set[str]) -> None:
    value = path.rstrip('/') if path not in {'', '/'} else path
    allowed_values = {item.rstrip('/') if item not in {'', '/'} else item for item in allowed}
    if value not in allowed_values:
        specific = sorted(item for item in allowed_values if item not in {'', '/'})
        suffix = f' or {specific[0]} URL' if specific else ''
        raise ValueError(f'{field} must be a router origin{suffix}')


def _positive_number(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{field} must be a positive finite number')
    return number


def _non_negative_number(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{field} must be a non-negative finite number')
    return number


def _int_between(value: Any, field: str, low: int, high: int) -> int:
    number = int(value)
    if number < low or number > high:
        raise ValueError(f'{field} must be between {low} and {high}')
    return number


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or '').strip()))


def _last(items: Sequence[Mapping[str, Any]], keys: tuple[str, ...]) -> Any:
    return next((item[key] for item in reversed(items) for key in keys if item.get(key)), '')


__all__ = ['RouterChatRequest', 'async_call_router_chat', 'call_router_chat']
