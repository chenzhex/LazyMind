from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from evo.artifact_flow.commands import (
    ApplyArtifactMutation, CancelFlow, ContinueFlow, PauseFlow, ResumeFlow, RetryFlow,
)
from evo.artifact_runtime.evo.actions import (
    EditArtifact, InvalidateFromStep, ReadCaseArtifact, ReadStepRoot, RerunCaseStage, RerunStep,
)
from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.service.runtime_port import RuntimePort

from .planner import plan_next_turn
from .schemas import MessageContentRef, MessageRequest, MessageTurnResult, PlannedAction
from .storage import MessageAuditStore, MessageBlobStore, json_bytes, new_turn_id

CONFIG_TARGETS = {
    'run_config': C.RUN_CONFIG,
    'source_config': C.CORPUS_SOURCE_CONFIG,
    'target_config': C.EVAL_TARGET_CONFIG,
    'eval_policy': C.EVAL_POLICY,
    'repair_policy': C.REPAIR_POLICY,
    'candidate_config': C.ABTEST_CANDIDATE_CONFIG,
}


class MessageTurnHandler:
    def __init__(self, root: Path, runtime: RuntimePort,
                 flow_runner: Callable[[str, Mapping[str, Any], object], object] | None = None) -> None:
        self.runtime = runtime
        self.flow_runner = flow_runner
        self.audit = MessageAuditStore(root)
        self.blobs = MessageBlobStore(root)

    def handle(self, thread_id: str, request: MessageRequest) -> MessageTurnResult:
        config = self.runtime.run_config(thread_id)
        if config is None:
            raise ValueError(f'thread not found: {thread_id}')
        message_id = request.message_id or f'msg_{uuid.uuid4().hex[:16]}'
        turn_id = new_turn_id()
        request_hash = self._hash(request.model_dump())
        replay = self.audit.begin_turn(thread_id, turn_id, message_id, request_hash)
        if replay and replay.result_ref:
            return MessageTurnResult.model_validate_json(
                self.blobs.load(replay.result_ref, thread_id),
            )
        if replay:
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                '该 message_id 已处理，但结果不可回放')
        try:
            msg_ref = self._blob(thread_id, turn_id, 'message_received', request.model_dump())
            self.audit.append_event(thread_id, turn_id, message_id, 'message_received', msg_ref, request.text)
            context = self._context(thread_id, turn_id, message_id, msg_ref, request, config)
            try:
                plan = plan_next_turn(context, config.get('llm_config') if isinstance(config, Mapping) else {})
            except Exception as exc:
                return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                    f'无法解析为结构化意图: {exc}')
            action = plan.next_action
            if action is None:
                return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                    '模型没有给出可执行的结构化动作')
            if action.kind in {'clarify', 'final'}:
                decision = 'needs_input' if action.kind == 'clarify' else 'final'
                return self._finish(thread_id, turn_id, message_id, decision,
                                    action.message or plan.response_hint)
            if action.kind == 'approval':
                return self._approval(thread_id, turn_id, message_id, action)
            if (context.get('projection') or {}).get('pending_approval_ref'):
                if plan.user_message_effect == 'cancel':
                    self.audit.update_projection(thread_id, pending_approval_ref=None)
                    return self._finish(thread_id, turn_id, message_id, 'rejected', '已取消待审批操作')
                if plan.user_message_effect not in {'amend', 'replace'}:
                    return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                        '仍有待确认操作；请明确确认、取消或修正。')
                self.audit.update_projection(thread_id, pending_approval_ref=None)
            reflected = False
            while True:
                try:
                    compiled, needs_approval = self._compile(thread_id, message_id, action, config)
                    break
                except Exception as exc:
                    if reflected or action.kind != 'config_patch':
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            f'结构化意图参数无效: {exc}')
                    reflected = True
                    context = {**context, 'config_validation_issue': {
                        'message': str(exc), 'failed_action': self._safe(action),
                    }}
                    try:
                        plan = plan_next_turn(context, config.get('llm_config') if isinstance(config, Mapping) else {})
                    except Exception as reflect_exc:
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            f'配置校验失败且反思修正失败: {reflect_exc}')
                    action = plan.next_action
                    if action is None or action.kind in {'clarify', 'final', 'approval'}:
                        text = getattr(action, 'message', '') if action is not None else plan.response_hint
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            text or f'配置校验失败: {exc}')
            action_ref = self._blob(thread_id, turn_id, 'compiled_action', compiled)
            self.audit.append_event(thread_id, turn_id, message_id, 'pending_action_recorded',
                                    action_ref, action.kind)
            if needs_approval:
                self.audit.update_projection(thread_id, pending_approval_ref=action_ref.model_dump())
                return self._finish(thread_id, turn_id, message_id, 'needs_approval',
                                    '需要确认后执行该操作', pending_approval_ref=action_ref)
            return self._dispatch(thread_id, turn_id, message_id, compiled, config)
        except Exception as exc:
            self.audit.abort_turn(thread_id, turn_id)
            if getattr(exc, 'status_code', 0) in {400, 404, 409, 422}:
                raise
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                f'message_intent 执行失败，需要补充或修正: {exc}')

    def _context(self, thread_id: str, turn_id: str, message_id: str, msg_ref: MessageContentRef,
                 request: MessageRequest, config: Mapping[str, Any]) -> dict[str, Any]:
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        snapshot = self.runtime.query(num_case).snapshot(thread_id)
        return {
            'thread_id': thread_id, 'run_id': thread_id, 'turn_id': turn_id, 'message_id': message_id,
            'user_message_ref': msg_ref.model_dump(), 'user_text': request.text,
            'client_context': request.client_context, 'projection': self.audit.projection(thread_id),
            'flow_snapshot': {
                'status': snapshot.status, 'pending_checkpoint': str(snapshot.pending_checkpoint),
                'progress': [item.__dict__ for item in snapshot.progress],
            },
            'steps': self.runtime.spec(num_case).steps,
        }

    def _compile(self, thread_id: str, message_id: str, action: PlannedAction,
                 config: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        spec = self.runtime.spec(num_case)
        command_id = self._command_id(thread_id, message_id, action)
        if action.kind == 'query':
            if action.query == 'progress_snapshot':
                return {'type': 'query', 'query': 'progress_snapshot'}, False
            if action.query == 'read_step_root':
                spec.read_step_root(action.step)
                return {'type': 'query', 'query': 'read_step_root', 'step': action.step}, False
            if action.query == 'read_case_artifact':
                spec.read_case_artifact(action.case_id, action.case_kind)
                return {'type': 'query', 'query': 'read_case_artifact', 'case_id': action.case_id,
                        'case_kind': action.case_kind}, False
            raise ValueError(f'unsupported query: {action.query}')
        if action.kind == 'flow':
            if action.command not in {'continue', 'pause', 'resume', 'cancel', 'retry'}:
                raise ValueError(f'unsupported flow command: {action.command}')
            if action.until_step:
                spec._require_step(action.until_step)
            return {'type': 'flow', 'command': action.command, 'until_step': action.until_step,
                    'command_id': command_id}, action.command in {'cancel'} or bool(action.until_step)
        if action.kind == 'mutation':
            return self._mutation(spec, action, command_id)
        if action.kind == 'config_patch':
            return self._config_patch(thread_id, action, command_id)
        raise ValueError(f'unsupported action kind: {action.kind}')

    def _mutation(self, spec, action: PlannedAction, command_id: str) -> tuple[dict[str, Any], bool]:
        if action.mutation == 'rerun_case_stage':
            spec.rerun_case_stage(action.case_id, action.stage)
        elif action.mutation == 'rerun_step':
            spec.rerun_step(action.step)
        elif action.mutation == 'invalidate_from_step':
            spec.jump_to_step(action.step)
        elif action.mutation == 'edit_artifact':
            artifact_ref = self._artifact_ref(action.artifact_ref)
            spec.edit_target(artifact_ref, action.pointer)
        else:
            raise ValueError(f'unsupported mutation: {action.mutation}')
        return {'type': 'mutation', **action.model_dump(), 'command_id': command_id}, True

    def _config_patch(self, thread_id: str, action: PlannedAction,
                      command_id: str) -> tuple[dict[str, Any], bool]:
        if not action.pointer.startswith('/'):
            raise ValueError('config_patch pointer must be an absolute JSON pointer')
        artifact_id = CONFIG_TARGETS[str(action.target)]
        ref = self.runtime.effective_ref(thread_id, artifact_id)
        if ref is None:
            raise ValueError(f'config artifact is not available: {action.target}')
        if action.target == 'run_config' and action.pointer in {'/thread_id', '/mode'}:
            raise ValueError(f'run_config field is immutable: {action.pointer}')
        if action.target == 'run_config' and action.pointer in {'/num_case', '/inputs/num_case'}:
            if not isinstance(action.value, int) or isinstance(action.value, bool) or action.value < 1:
                raise ValueError('num_case must be a positive integer')
        payload = {'kind': 'mutation', 'mutation': 'edit_artifact',
                   'artifact_ref': [ref.key.artifact_id, ref.key.partition, ref.version],
                   'pointer': action.pointer, 'value': action.value,
                   'config_target': action.target, 'command_id': command_id}
        return {'type': 'mutation', **payload}, True

    def _approval(self, thread_id: str, turn_id: str, message_id: str,
                  action: PlannedAction) -> MessageTurnResult:
        pending = self.audit.projection(thread_id).get('pending_approval_ref') or {}
        if action.decision == 'reject':
            self.audit.update_projection(thread_id, pending_approval_ref=None)
            return self._finish(thread_id, turn_id, message_id, 'rejected', '已取消待审批操作')
        if action.decision != 'approve' or not pending:
            return self._finish(thread_id, turn_id, message_id, 'needs_input', '没有可确认的待审批操作')
        ref = MessageContentRef.model_validate(pending)
        compiled = json.loads(self.blobs.load(ref, thread_id))
        self.audit.update_projection(thread_id, pending_approval_ref=None)
        config = self.runtime.run_config(thread_id) or {}
        return self._dispatch(thread_id, turn_id, message_id, compiled, config)

    def _dispatch(self, thread_id: str, turn_id: str, message_id: str, compiled: Mapping[str, Any],
                  config: Mapping[str, Any]) -> MessageTurnResult:
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        if compiled['type'] == 'query':
            result = self._read(thread_id, num_case, compiled)
            turn_decision = event_kind = 'query_answered'
            text = '已读取当前信息，详细结果已写入 observation。'
        else:
            command = self._command(compiled)
            if self.flow_runner is not None:
                result = self.flow_runner(thread_id, config, command)
            else:
                result = self.runtime.flow(num_case).handle(thread_id, command)
            status = str(getattr(result, 'command_status', 'ok'))
            event_kind = 'action_executed' if status == 'ok' else 'action_failed'
            turn_decision = 'action_executed' if status == 'ok' else 'needs_input'
            error = str(getattr(result, 'error', '') or '')
            text = '已执行该操作。' if status == 'ok' else f'操作未完成，状态为 {status}: {error}'.rstrip(': ')
        ref = self._blob(thread_id, turn_id, 'action_receipt', self._safe(result))
        self.audit.record_receipt(thread_id, message_id, self._hash(compiled), str(compiled.get('command_id') or ''),
                                  event_kind, ref)
        self.audit.append_event(thread_id, turn_id, message_id, event_kind, ref, event_kind)
        snapshot = self.runtime.query(num_case).snapshot(thread_id)
        obs = self._blob(thread_id, turn_id, 'observation', self._safe(snapshot))
        self.audit.update_projection(thread_id, last_observation_ref=obs.model_dump(), last_observation_hash=obs.sha256)
        return self._finish(thread_id, turn_id, message_id, turn_decision, text, observation_ref=obs,
                            action_receipt_ref=ref)

    def _read(self, thread_id: str, num_case: int, compiled: Mapping[str, Any]) -> Any:
        query = compiled['query']
        if query == 'progress_snapshot':
            return self.runtime.query(num_case).snapshot(thread_id)
        if query == 'read_step_root':
            return self.runtime.query(num_case).read(thread_id, ReadStepRoot(str(compiled['step'])))
        return self.runtime.query(num_case).read(
            thread_id, ReadCaseArtifact(str(compiled['case_id']), str(compiled['case_kind'])),
        )

    def _command(self, compiled: Mapping[str, Any]):
        command_id = str(compiled.get('command_id') or '')
        if compiled['type'] == 'flow':
            return {
                'continue': ContinueFlow(command_id, str(compiled.get('until_step') or '')),
                'pause': PauseFlow(command_id), 'resume': ResumeFlow(command_id),
                'cancel': CancelFlow(command_id), 'retry': RetryFlow(command_id),
            }[str(compiled['command'])]
        mutation = str(compiled['mutation'])
        if mutation == 'rerun_case_stage':
            action = RerunCaseStage(str(compiled['case_id']), str(compiled['stage']), command_id)
        elif mutation == 'rerun_step':
            action = RerunStep(str(compiled['step']), command_id)
        elif mutation == 'invalidate_from_step':
            action = InvalidateFromStep(str(compiled['step']), command_id)
        else:
            action = EditArtifact(self._artifact_ref(compiled['artifact_ref']), str(compiled['pointer']),
                                  compiled.get('value'), command_id)
        return ApplyArtifactMutation(command_id, action)

    def _finish(self, thread_id: str, turn_id: str, message_id: str, decision: str, text: str,
                **refs: Any) -> MessageTurnResult:
        text_ref = self._blob(thread_id, turn_id, 'assistant_text', {'text': text})
        self.audit.append_event(thread_id, turn_id, message_id, 'assistant_response', text_ref, text)
        result = MessageTurnResult(thread_id=thread_id, turn_id=turn_id, message_id=message_id,
                                   turn_decision=decision, assistant_text=text,
                                   assistant_text_ref=text_ref, **refs)
        result_ref = self._blob(thread_id, turn_id, 'turn_result', result.model_dump())
        self.audit.finish_turn(thread_id, turn_id, result_ref)
        return result

    def _blob(self, thread_id: str, turn_id: str, kind: str, value: object) -> MessageContentRef:
        return self.blobs.append(thread_id, turn_id, kind, json_bytes(self._safe(value)))

    @classmethod
    def _command_id(cls, thread_id: str, message_id: str, action: PlannedAction) -> str:
        return f'msgi:{thread_id}:{message_id}:{cls._hash(action.model_dump())[:24]}'

    @staticmethod
    def _artifact_ref(value: Any) -> ArtifactRef:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError('artifact_ref must be [artifact_id, partition, version]')
        return ArtifactRef(ArtifactKey(str(value[0]), str(value[1])), int(value[2]))

    @classmethod
    def _hash(cls, value: object) -> str:
        return hashlib.sha256(json_bytes(cls._safe(value))).hexdigest()

    @classmethod
    def _safe(cls, value: object) -> object:
        if hasattr(value, '__dict__'):
            return {key: cls._safe(item) for key, item in vars(value).items()}
        if isinstance(value, Mapping):
            return {str(key): cls._safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe(item) for item in value]
        return value
