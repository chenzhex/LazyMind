from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schemas import MessageContentRef


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


class MessageConflictError(RuntimeError):
    pass


class MessageInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class TurnReplay:
    result_ref: MessageContentRef | None = None


TURN_STALE_SECONDS = 900


class MessageBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.blob_root = root / 'message-store' / 'blobs'
        self.blob_root.mkdir(parents=True, exist_ok=True)

    def append(self, thread_id: str, turn_id: str, kind: str, payload: bytes,
               mime_type: str = 'application/json') -> MessageContentRef:
        digest = hashlib.sha256(payload).hexdigest()
        safe = ''.join(c if c.isalnum() or c in '._-' else '_' for c in kind)[:48] or 'blob'
        rel = Path('message-store') / 'blobs' / digest[:2] / self._hash(thread_id) / turn_id
        folder = self.root / rel
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.tmp-', suffix='.part', dir=folder)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            target = folder / f'{int(time.time() * 1000)}-{safe}-{digest[:12]}.blob'
            os.replace(tmp, target)
            self._fsync_dir(folder)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return MessageContentRef(uri=str(target.relative_to(self.root)), sha256=digest,
                                 byte_size=len(payload), mime_type=mime_type)

    def load(self, ref: MessageContentRef, thread_id: str = '') -> bytes:
        path = (self.root / ref.uri).resolve()
        if not path.is_file() or not path.is_relative_to(self.blob_root.resolve()):
            raise ValueError('message blob path is outside message-store')
        if thread_id and self._hash(thread_id) not in path.parts:
            raise ValueError('message blob belongs to a different thread')
        payload = path.read_bytes()
        if len(payload) != ref.byte_size or hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ValueError('message blob checksum mismatch')
        return payload

    def delete_thread(self, thread_id: str) -> None:
        marker = self._hash(thread_id)
        for path in self.blob_root.glob(f'*/{marker}'):
            if path.is_dir() and path.resolve().is_relative_to(self.blob_root.resolve()):
                shutil.rmtree(path)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class MessageAuditStore:
    def __init__(self, root: Path) -> None:
        self.db = root / 'artifact-store' / 'artifact_store.sqlite3'
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def begin_turn(self, thread_id: str, turn_id: str, message_id: str,
                   request_hash: str) -> TurnReplay | None:
        if not message_id:
            return None
        with self._connect() as conn:
            conn.execute('begin immediate')
            row = conn.execute(
                'select turn_id, request_sha256, turn_decision, result_uri, result_sha256, '
                'result_byte_size, updated_at from message_turns where thread_id = ? and message_id = ?',
                (thread_id, message_id),
            ).fetchone()
            if row is not None:
                if row[1] != request_hash:
                    raise MessageConflictError('message_id reused with different payload')
                if row[2] != 'done':
                    if row[2] == 'open' and time.time() - float(row[6] or 0) < TURN_STALE_SECONDS:
                        raise MessageInProgressError('message turn is still in progress')
                    conn.execute(
                        'update message_turns set turn_id = ?, turn_decision = ?, first_event_seq = ?, '
                        'last_event_seq = ?, result_uri = ?, result_sha256 = ?, result_byte_size = ?, '
                        'updated_at = ? where thread_id = ? and message_id = ?',
                        (turn_id, 'open', 0, 0, '', '', 0, time.time(), thread_id, message_id),
                    )
                    return None
                if row[3]:
                    return TurnReplay(MessageContentRef(uri=row[3], sha256=row[4],
                                                        byte_size=row[5]))
                return TurnReplay()
            conn.execute(
                'insert into message_turns(thread_id, turn_id, message_id, request_sha256, '
                'turn_decision, schema_version, model_id, first_event_seq, last_event_seq, '
                'result_uri, result_sha256, result_byte_size, created_at, updated_at) '
                'values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (thread_id, turn_id, message_id, request_hash, 'open', 'message_intent.v1',
                 '', 0, 0, '', '', 0, time.time(), time.time()),
            )
        return None

    def abort_turn(self, thread_id: str, turn_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                'update message_turns set turn_decision = ?, updated_at = ? '
                'where thread_id = ? and turn_id = ? and turn_decision = ?',
                ('failed', time.time(), thread_id, turn_id, 'open'),
            )

    def finish_turn(self, thread_id: str, turn_id: str, result_ref: MessageContentRef) -> None:
        with self._connect() as conn:
            conn.execute(
                'update message_turns set turn_decision = ?, result_uri = ?, result_sha256 = ?, '
                'result_byte_size = ?, updated_at = ? where thread_id = ? and turn_id = ?',
                ('done', result_ref.uri, result_ref.sha256, result_ref.byte_size, time.time(),
                 thread_id, turn_id),
            )

    def append_event(self, thread_id: str, turn_id: str, message_id: str, kind: str,
                     ref: MessageContentRef, short_text: str = '') -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                'insert into message_events(thread_id, turn_id, message_id, event_kind, blob_uri, blob_sha256, '
                'blob_byte_size, mime_type, schema_version, short_text, created_at) '
                'values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (thread_id, turn_id, message_id, kind, ref.uri, ref.sha256, ref.byte_size, ref.mime_type,
                 'message_intent.v1', short_text[:1024], time.time()),
            )
            seq = int(cursor.lastrowid)
            conn.execute(
                'update message_turns set first_event_seq = case when first_event_seq = 0 then ? '
                'else first_event_seq end, last_event_seq = ?, updated_at = ? '
                'where thread_id = ? and turn_id = ?',
                (seq, seq, time.time(), thread_id, turn_id),
            )
            return seq

    def projection(self, thread_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                'select active_agenda_ref, pending_input_ref, pending_approval_ref, '
                'last_observation_ref, last_observation_hash from message_projection where thread_id = ?',
                (thread_id,),
            ).fetchone()
        if row is None:
            return {}
        return {
            'active_agenda_ref': self._json(row[0]),
            'pending_input_ref': self._json(row[1]),
            'pending_approval_ref': self._json(row[2]),
            'last_observation_ref': self._json(row[3]),
            'last_observation_hash': row[4],
        }

    def update_projection(self, thread_id: str, **values: Any) -> None:
        with self._connect() as conn:
            conn.execute('begin immediate')
            row = conn.execute(
                'select active_agenda_ref, pending_input_ref, pending_approval_ref, '
                'last_observation_ref, last_observation_hash from message_projection where thread_id = ?',
                (thread_id,),
            ).fetchone()
            current = {} if row is None else {
                'active_agenda_ref': self._json(row[0]),
                'pending_input_ref': self._json(row[1]),
                'pending_approval_ref': self._json(row[2]),
                'last_observation_ref': self._json(row[3]),
                'last_observation_hash': row[4],
            }
            current.update(values)
            conn.execute(
                'insert or replace into message_projection values (?, ?, ?, ?, ?, ?, ?)',
                (thread_id, self._dump(current.get('active_agenda_ref')),
                 self._dump(current.get('pending_input_ref')), self._dump(current.get('pending_approval_ref')),
                 self._dump(current.get('last_observation_ref')), str(current.get('last_observation_hash') or ''),
                 time.time()),
            )

    def record_receipt(self, thread_id: str, message_id: str, action_hash: str, command_id: str,
                       outcome: str, ref: MessageContentRef) -> None:
        with self._connect() as conn:
            conn.execute(
                'insert or replace into message_receipts values (?, ?, ?, ?, ?, ?, ?, ?)',
                (thread_id, message_id, action_hash, command_id, outcome, ref.uri, ref.sha256, time.time()),
            )

    def delete_thread(self, thread_id: str) -> None:
        with self._connect() as conn:
            for table in ('message_receipts', 'message_projection', 'message_turns', 'message_events'):
                conn.execute(f'delete from {table} where thread_id = ?', (thread_id,))

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                create table if not exists message_events(
                    event_seq integer primary key, thread_id text not null, turn_id text not null,
                    message_id text not null, event_kind text not null, blob_uri text not null,
                    blob_sha256 text not null, blob_byte_size integer not null, mime_type text not null,
                    schema_version text not null, short_text text not null, created_at real not null);
                create table if not exists message_turns(
                    thread_id text not null, turn_id text not null, message_id text not null,
                    request_sha256 text not null, turn_decision text not null,
                    schema_version text not null, model_id text not null,
                    first_event_seq integer not null, last_event_seq integer not null,
                    result_uri text not null, result_sha256 text not null,
                    result_byte_size integer not null, created_at real not null, updated_at real not null,
                    primary key(thread_id, turn_id), unique(thread_id, message_id));
                create table if not exists message_projection(
                    thread_id text primary key, active_agenda_ref text not null, pending_input_ref text not null,
                    pending_approval_ref text not null, last_observation_ref text not null,
                    last_observation_hash text not null, updated_at real not null);
                create table if not exists message_receipts(
                    thread_id text not null, message_id text not null, action_hash text not null,
                    command_id text not null, outcome_kind text not null, receipt_uri text not null,
                    receipt_sha256 text not null, created_at real not null,
                    primary key(thread_id, message_id, action_hash));
            """)
            columns = {
                row[1] for row in conn.execute('pragma table_info(message_turns)').fetchall()
            }
            if 'request_sha256' not in columns:
                conn.execute('drop table message_turns')
                conn.execute("""
                    create table message_turns(
                        thread_id text not null, turn_id text not null, message_id text not null,
                        request_sha256 text not null, turn_decision text not null,
                        schema_version text not null, model_id text not null,
                        first_event_seq integer not null, last_event_seq integer not null,
                        result_uri text not null, result_sha256 text not null,
                        result_byte_size integer not null, created_at real not null, updated_at real not null,
                        primary key(thread_id, turn_id), unique(thread_id, message_id))
                """)

    def _connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.execute('pragma journal_mode=wal')
        conn.execute('pragma busy_timeout=30000')
        return conn

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def _json(value: str) -> Any:
        return json.loads(value) if value else {}


def new_turn_id() -> str:
    return f'turn_{uuid.uuid4().hex[:16]}'
