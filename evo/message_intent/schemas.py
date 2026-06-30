from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class MessageContentRef(StrictModel):
    uri: str = Field(max_length=512)
    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int
    mime_type: str = Field(default='application/json', max_length=80)
    compression: str = Field(default='', max_length=24)


class MessageRequest(StrictModel):
    schema_version: Literal['message_request.v1'] = 'message_request.v1'
    message_id: str = Field(default='', max_length=160)
    text: str = Field(min_length=1, max_length=20000)
    attachments: list[MessageContentRef] = Field(default_factory=list, max_length=4)
    client_context: dict[str, Any] = Field(default_factory=dict)


class FlowAction(StrictModel):
    kind: Literal['flow']
    command: Literal['continue', 'pause', 'resume', 'cancel', 'retry']
    until_step: str = ''


class QueryAction(StrictModel):
    kind: Literal['query']
    query: Literal['progress_snapshot', 'read_step_root', 'read_case_artifact']
    step: str = ''
    case_id: str = ''
    case_kind: str = ''

    @model_validator(mode='after')
    def validate_query(self) -> 'QueryAction':
        if self.query == 'read_step_root' and not self.step:
            raise ValueError('step is required for read_step_root')
        if self.query == 'read_case_artifact' and (not self.case_id or not self.case_kind):
            raise ValueError('case_id and case_kind are required for read_case_artifact')
        return self


class MutationAction(StrictModel):
    kind: Literal['mutation']
    mutation: Literal['edit_artifact', 'rerun_case_stage', 'rerun_step', 'invalidate_from_step']
    step: str = ''
    case_id: str = ''
    stage: str = ''
    artifact_ref: list[Any] = Field(default_factory=list)
    pointer: str = ''
    value: Any = None

    @model_validator(mode='after')
    def validate_mutation(self) -> 'MutationAction':
        if self.mutation == 'rerun_case_stage' and (not self.case_id or not self.stage):
            raise ValueError('case_id and stage are required for rerun_case_stage')
        if self.mutation in {'rerun_step', 'invalidate_from_step'} and not self.step:
            raise ValueError('step is required for step mutation')
        if self.mutation == 'edit_artifact' and (not self.artifact_ref or not self.pointer):
            raise ValueError('artifact_ref and pointer are required for edit_artifact')
        return self


class ConfigPatchAction(StrictModel):
    kind: Literal['config_patch']
    target: Literal['run_config', 'source_config', 'target_config', 'eval_policy',
                    'repair_policy', 'candidate_config']
    pointer: str
    value: Any = None
    message: str = ''


class ApprovalAction(StrictModel):
    kind: Literal['approval']
    decision: Literal['approve', 'reject']
    message: str = ''


class ClarifyAction(StrictModel):
    kind: Literal['clarify']
    message: str = ''


class FinalAction(StrictModel):
    kind: Literal['final']
    message: str = ''


PlannedAction: TypeAlias = Annotated[
    FlowAction | QueryAction | MutationAction | ConfigPatchAction | ApprovalAction | ClarifyAction | FinalAction,
    Field(discriminator='kind'),
]


class TurnPlan(StrictModel):
    schema_version: Literal['message_intent.v1'] = 'message_intent.v1'
    turn_decision: Literal['next_action', 'needs_input', 'needs_approval', 'final']
    active_agenda: list[dict[str, Any]] = Field(default_factory=list)
    next_action: PlannedAction | None = None
    user_message_effect: Literal['append', 'amend', 'replace', 'cancel', 'none'] = 'none'
    response_hint: str = ''

    @model_validator(mode='after')
    def validate_decision(self) -> 'TurnPlan':
        action = self.next_action
        if self.turn_decision == 'next_action':
            if action is None or action.kind in {'clarify', 'final', 'approval'}:
                raise ValueError('next_action turn requires executable flow/query/mutation/config_patch action')
        elif self.turn_decision == 'needs_input':
            if action is not None and action.kind != 'clarify':
                raise ValueError('needs_input turn cannot carry executable action')
            self.next_action = action or ClarifyAction(message=self.response_hint)
        elif self.turn_decision == 'final':
            if action is not None and action.kind != 'final':
                raise ValueError('final turn cannot carry executable action')
            self.next_action = action or FinalAction(message=self.response_hint)
        elif action is None or action.kind != 'approval':
            raise ValueError('needs_approval turn requires approval action')
        return self


class MessageTurnResult(StrictModel):
    schema_version: Literal['message_turn_result.v1'] = 'message_turn_result.v1'
    thread_id: str
    turn_id: str
    message_id: str
    turn_decision: Literal[
        'needs_input', 'needs_approval', 'action_executed', 'query_answered', 'final', 'rejected'
    ]
    assistant_text: str = ''
    assistant_text_ref: MessageContentRef | None = None
    observation_ref: MessageContentRef | None = None
    pending_input_ref: MessageContentRef | None = None
    pending_approval_ref: MessageContentRef | None = None
    action_receipt_ref: MessageContentRef | None = None
