from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import FileResponse

from evo.message_intent import MessageRequest, MessageTurnHandler
from evo.message_intent.storage import MessageConflictError, MessageInProgressError

from .projections import ProjectionService
from .threads import ThreadService


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ThreadInputs(StrictModel):
    kb_id: list[str] = Field(default_factory=list)
    csv_data: list[dict[str, str]] = Field(default_factory=list)
    target_chat_url: str
    num_case: int = Field(gt=0)
    case_deadline_seconds: float = Field(default=60.0, gt=0)


class ThreadCreate(StrictModel):
    mode: str
    title: str = ''
    inputs: ThreadInputs
    llm_config: dict[str, Any]


class CommandRequest(StrictModel):
    command_id: str = ''
    until_step: str = ''


class EmptyCommandRequest(StrictModel):
    command_id: str = ''


class MessageBody(StrictModel):
    message_id: str = Field(default='', max_length=160)
    text: str = Field(default='', max_length=20000)
    content: str = Field(default='', max_length=20000)
    client_context: dict[str, Any] = Field(default_factory=dict)


CommandBody = Annotated[CommandRequest, Body()]
EmptyCommandBody = Annotated[EmptyCommandRequest, Body()]
MessageRequestBody = Annotated[MessageBody, Body()]


class EvoService:
    def __init__(self, root: Path) -> None:
        self.threads = ThreadService(root)
        self.projections = ProjectionService(root, self.threads.runtime)
        self.messages = MessageTurnHandler(root, self.threads.runtime, self.threads.run_message_command)


def create_app() -> FastAPI:
    root = _service_root()
    service = EvoService(root)
    app = FastAPI(title='evo service', version='v0.3.0')
    app.state.service = service

    @app.get('/healthz')
    def healthz() -> dict[str, bool]:
        return {'ok': True}

    @app.get('/livez')
    def livez() -> dict[str, bool]:
        return {'alive': True}

    @app.get('/readyz')
    def readyz() -> dict[str, bool]:
        return {'ready': True}

    @app.post('/threads')
    def create_thread(payload: ThreadCreate) -> dict[str, Any]:
        return service.threads.create(payload.model_dump())

    @app.get('/threads')
    def list_threads(
        page_size: Annotated[int, Query(ge=1, le=200)] = 10,
        page_token: str = '',
        status: str = '',
    ) -> dict[str, Any]:
        return service.threads.list(page_size, page_token, status)

    @app.get('/threads/{thread_id}')
    def get_thread(thread_id: str) -> dict[str, Any]:
        return service.threads.public_thread(thread_id)

    @app.delete('/threads/{thread_id}')
    def delete_thread(thread_id: str) -> dict[str, Any]:
        return service.threads.delete(thread_id)

    @app.post('/threads/{thread_id}/start')
    def start_thread(
        thread_id: str,
        payload: CommandBody,
        background_tasks: BackgroundTasks,
    ) -> dict[str, str]:
        return service.threads.start(thread_id, payload.model_dump(), background_tasks.add_task)

    @app.post('/threads/{thread_id}/continue')
    def continue_thread(
        thread_id: str,
        payload: CommandBody,
        background_tasks: BackgroundTasks,
    ) -> dict[str, str]:
        return service.threads.continue_thread(thread_id, payload.model_dump(), background_tasks.add_task)

    @app.post('/threads/{thread_id}/retry')
    def retry_thread(
        thread_id: str,
        payload: CommandBody,
        background_tasks: BackgroundTasks,
    ) -> dict[str, str]:
        return service.threads.retry(thread_id, payload.model_dump(), background_tasks.add_task)

    @app.post('/threads/{thread_id}/pause')
    def pause_thread(
        thread_id: str,
        payload: EmptyCommandBody,
    ) -> dict[str, str]:
        return service.threads.pause(thread_id, payload.model_dump())

    @app.post('/threads/{thread_id}/cancel')
    def cancel_thread(
        thread_id: str,
        payload: EmptyCommandBody,
    ) -> dict[str, str]:
        return service.threads.cancel(thread_id, payload.model_dump())

    @app.get('/threads/{thread_id}/gates')
    def gates(thread_id: str) -> dict[str, Any]:
        return service.projections.gates(thread_id)

    @app.get('/threads/{thread_id}/gates/{step}/versions/{version}:download')
    def gate_download(thread_id: str, step: str, version: int, format: str = 'json') -> FileResponse:  # noqa: A002
        path = service.projections.gate_download(thread_id, step, version, format)
        return FileResponse(path, filename=path.name, media_type='application/octet-stream')

    @app.get('/threads/{thread_id}/gates/{step}/versions/{version}')
    def gate_content(thread_id: str, step: str, version: int) -> dict[str, Any]:
        return service.projections.gate_content(thread_id, step, version)

    @app.get('/threads/{thread_id}/events')
    def events(
        thread_id: str,
        step: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        return service.projections.events(thread_id, step, after, limit)

    @app.post('/threads/{thread_id}/messages')
    def messages(thread_id: str, payload: MessageRequestBody) -> dict[str, Any]:
        text = payload.text or payload.content
        if not text.strip():
            raise HTTPException(422, 'text or content is required')
        msg = MessageRequest(message_id=payload.message_id, text=text, client_context=payload.client_context)
        result = _message_result(service, thread_id, msg)
        return result.model_dump()

    @app.patch('/threads/{thread_id}/artifacts/{artifact_ref:path}')
    def mutate_artifact(thread_id: str, artifact_ref: str, response: Response) -> dict[str, str]:
        response.status_code = 501
        return {'status': 'not_implemented', 'message': 'artifact mutation is not enabled'}

    @app.get('/candidates')
    def candidates(
        thread_id: str = '',
        status: str = '',
        page_size: Annotated[int, Query(ge=1, le=200)] = 20,
        page_token: str = '',
    ) -> dict[str, Any]:
        return service.projections.candidates(thread_id, status, page_size, page_token)

    @app.get('/candidates/{candidate_id:path}')
    def candidate(candidate_id: str) -> dict[str, Any]:
        return service.projections.candidate(candidate_id)

    return app


def get_app() -> FastAPI:
    return create_app()


def _service_root() -> Path:
    configured = os.getenv('LAZYMIND_EVO_BASE_DIR')
    if configured:
        return Path(configured)
    root = Path('/var/lib/lazymind/evo')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _message_result(service: EvoService, thread_id: str, request: MessageRequest):
    try:
        return service.messages.handle(thread_id, request)
    except MessageConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except MessageInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc).lower():
            raise HTTPException(409, 'message store is busy; retry the same message_id') from exc
        raise
    except ValueError as exc:
        message = str(exc)
        if message.startswith('thread not found'):
            raise HTTPException(404, message) from exc
        raise HTTPException(422, message) from exc
