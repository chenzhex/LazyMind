# message_intent artifact-centric 设计方案

本文是 `LazyRAG/evo` 新主干的 `message_intent` 设计边界。结论先写在前面：

`message_intent` 只做自然语言消息到现有 `FlowCommand` / `EvoMutation` / `EvoQuery` 的模型结构化翻译、校验、审计和 turn 级协调。它不是 workflow engine，不维护 step 业务状态，不判断 artifact 是否有效，不直接推进 runtime。

## 0. 设计前提

### 已确认主干边界

- `usage.md` 的核心原则仍然成立：Evo 是 artifact-centric，流程推进由 artifact 是否存在、是否为当前 effective ref 决定。
- `artifact_runtime.kernel` 只负责 artifact、graph、runtime、store、materializer，不知道 message intent。
- `artifact_runtime.evo` 只暴露 Evo 领域的 artifact/action/use case。
- `artifact_flow` 已经拥有 `ContinueFlow`、`PauseFlow`、`ResumeFlow`、`CancelFlow`、`RetryFlow`、`ApplyArtifactMutation`、命令 receipt、幂等和 checkpoint gate。
- `service/api.py` 代码已经比 `usage.md` 的“只保留 healthz”更往前：已有 `/threads`、`/continue`、`/pause`、`/messages` 501 占位。这是文档漂移，不是 message_intent 可以绕开 Flow 的理由。

### 用户约束

- 解析用户指令时不能使用规则匹配、关键词匹配、正则路由、相似度路由。
- 用户可以在解析中或执行单个任务时追加消息，用来补充、修正、取消或替换之前意图。
- message 正文、模型结构化输出、长 observation、assistant 长回复倾向文件存储；SQLite 只存索引和事务性 receipt。
- 配置参数可能由模型或用户给错，message_intent 需要先校验、让模型反思修正，再提交配置变更；不能把底层 ValueError 直接甩给用户。
- 方案必须基于当前 artifact-centric 主干，不能长出第二套状态机、流程调度或业务状态源。
- 实现要依赖可靠第三方能力，拒绝手搓 JSON/schema/patch/path/workflow 兜底。

## 1. 非目标和硬边界

### 禁止做的事

- 禁止在 `message_intent` 里直接调用 `adapter.tick(...)`、`graph.next_ops(...)`、runtime 或 materializer。
- 禁止直接写 `SQLiteArtifactStore.commit_external(...)`、`invalidate(...)`、`delete_*`；artifact 写入必须变成 `EvoMutation` 并经 `ApplyArtifactMutation` 进入 `FlowService`。
- 禁止 import 或调用 `evo.operations.*`。
- 禁止直接读写 `flow_gates`、`flow_command_receipts`，除非通过现有 `FlowService` / `FlowQueryService`。
- 禁止持久化 authoritative `current_step`、`completed_steps`、`running_ops`、artifact effective ref、candidate 状态、step status。
- 禁止把 pending agenda 当作可驱动 runtime 的 workflow state；每次执行前必须重新 observation + compile + validate。
- 禁止用 `json-repair`、关键词字典、正则、rapidfuzz、embedding 相似度等方式修复或猜测 intent。

### 允许做的事

- 调用 `FlowQueryService.snapshot/progress/read` 获得只读 observation。
- 调用 `FlowService.handle(run_id, FlowCommand)` 发送 flow 命令。
- 把模型结构化输出编译为现有 `EvoQuery`、`EvoMutation`、`FlowCommand`。
- 持久化 message-turn 审计事件、blob 指针和 turn 级控制投影，例如 message/model plan blob ref、pending_input、pending_approval、command receipt 摘要。
- 为 UI 返回 assistant 文案、clarification、approval preview。

## 2. 状态边界

`message_intent` 可以有“会话控制元数据”，但它不是 Evo 业务状态。

### 可以持久化

只采用方案 A：复用主干 `<evo-root>/artifact-store/artifact_store.sqlite3`，在同一个 SQLite 文件内新增 `message_*` 小表；消息正文、模型结构化输出、长 observation、assistant 长回复、approval preview、错误诊断材料一律写入 `<evo-root>/message-store/blobs` 文件 blob。

禁止新增任何 message 专用 SQLite 文件，尤其不能新增 `message_index.sqlite3`。`message_intent` 不拥有 SQLite 数据库，只使用主干 artifact store 的 SQLite path/connection 创建允许的 `message_*` 表。

`message_*` 表 allow-list：

- `message_events`：append-only message 事件索引，保存 `event_seq`、`thread_id`、`turn_id`、`message_id`、`event_kind`、`blob_uri`、`blob_sha256`、`blob_byte_size`、`mime_type`、`schema_version`、短摘要、`created_at`。它只回答“发生过什么消息层事件”，不表达 Evo 业务进度。
- `message_turns`：turn 生命周期和 replay identity，保存 `turn_id`、`thread_id`、`message_id`、`turn_decision`、schema/model 版本、first/last event seq、创建/结束时间。它只用于消息请求幂等和 UI 展示，不保存 step/runtime 状态。
- `message_projection`：可重建、可丢弃的 turn 控制投影，保存 `thread_id`、`active_agenda_ref`、`pending_input_ref`、`pending_approval_ref`、`last_observation_ref`、`last_observation_hash`、更新时间。它不是业务事实源，删除后必须能从 message events + 当前 FlowQuery observation 重建。
- `message_receipts`：message action replay/conflict 摘要，保存 `thread_id`、`message_id`、`agenda_item_id`、`action_hash`、`command_id`、`outcome_kind`、`flow_receipt_ref`、短摘要、`created_at`。它只包一层 message 幂等审计，权威 flow receipt 仍在 `FlowService` / FlowGate。

`message_*` 表禁止保存：

- `raw_message`、`content_json`、完整 prompt、完整 model plan、长 observation、assistant 长回复、approval preview 大对象。
- artifact payload 副本、pickle payload path、`artifact_id/partition/version` 作为 head/effective 索引。
- `current_step`、`completed_steps`、`running_ops`、`step_status`、candidate 状态、repair/abtest 业务状态。
- `effective_ref`、`artifact_heads` 镜像、`input_refs_json`/provenance 镜像。
- `flow_gates.status`、`pending_checkpoint_json`、`released_checkpoints_json` 的权威副本。
- secret 原值或未 redaction 的 API key/config。

索引行只能保存 blob 指针和小摘要：`uri`、`sha256`、`byte_size`、`mime_type`、`schema_version`、`short_text`/`summary_json`。完整内容必须在 `message-store/blobs`，并在读取时校验 hash 和大小。

硬边界：

- message blob 没有 `ArtifactKey`。
- message blob 没有 `ArtifactRef`。
- message blob 没有 version/head/effective/provenance。
- message blob 没有 `commit_external` / `commit_outputs` / `invalidate`。
- message blob GC 不调用 `SQLiteArtifactStore.gc()`。
- message blob 不放入 `artifact-store/payloads`。
- message audit corruption 最多影响消息回放和 UI 审计，不影响 Evo artifact run。

这些数据只能回答：

- 用户说过什么。
- 模型当时解析成什么结构。
- 系统发出了哪个既有 Evo 命令。
- 该命令返回了什么 receipt 摘要。
- 当前 turn 是否还在等用户补充或审批。

### 不可以持久化

- 当前真实 step。
- 哪些 artifact 已完成、有效或 stale。
- 哪些 op 待执行或已执行。
- artifact payload 的副本。
- repair/abtest/candidate 的业务状态。
- 用于恢复 runtime 执行的自定义 workflow checkpoint。
- 将文件 blob 注册成 Evo artifact ref 或业务产物版本。

真实业务事实只在 artifact store；真实流程门闩只在 FlowGate；`message_intent` 的 projection 可以丢弃后通过 message events 和当前 flow/artifact observation 重建。

### 文件布局

建议放在 service root 下，与 artifact store 平级，避免混入 `artifact-store/payloads`：

```text
<evo-root>/
  artifact-store/
    artifact_store.sqlite3
    payloads/
  message-store/
    blobs/
      <thread_hash_prefix>/
        <thread_hash>/
          <turn_id>/
            tmp/
            events/
              <event_seq>-<kind>-<sha256_prefix>.json
              <event_seq>-<kind>-<sha256_prefix>.txt
            approvals/
```

第一版固定使用 thread/turn-local 布局：排查简单，`delete_thread` 可直接删除该 thread hash 目录。不要在第一版引入全局 content-addressed message blob store，避免被误解成第二套 artifact payload store。

写入规则：

- 先写 `tmp/<uuid>.part`。
- flush + fsync 文件。
- rename 到最终路径。
- fsync 父目录。
- 再写主干 `artifact_store.sqlite3` 中的 `message_*` index，推荐 `BEGIN IMMEDIATE`，事务尽量短。
- SQLite 事务内禁止模型调用、`FlowService` 调用、文件 hash/大文件 IO、长 observation 构造。
- 读取时校验 `sha256` 和 `byte_size`；不匹配视为 message log corruption，只影响消息层，不影响 Evo artifact run。
- index 写入失败时，未索引 blob 可由 GC 清理。
- blob 删除只由 message log GC 执行，不删除 artifact payload。

`delete_thread` 规则：

- 删除该 thread 的 `message_*` rows。
- 删除 `message-store/blobs/<thread_hash_prefix>/<thread_hash>/`。
- 失败时记录可重试 tombstone。
- 不触碰 `artifact-store/payloads`，不影响 artifact records/heads/events，也不影响 FlowGate 正常删除路径。

`delete_run` / GC 边界：

- artifact GC 只看 `artifact_records.payload_path`，不扫描 `message-store`。
- message GC 只看 `message_*` live refs 和 `message-store/blobs`，不调用 `SQLiteArtifactStore.gc()`。
- `RuntimePort.delete_run()` 若要清 message，只能作为显式第三段 cleanup：删除相关 `message_*` rows 和 message blob 目录；不能把 message blob 纳入 artifact payload GC。

## 3. 行业范式取舍

### 采用

- OpenAI Structured Outputs / function calling 的范式：模型输出必须服从 JSON Schema 或 tool-args schema，代码只执行通过 schema 校验的 typed intent。
- Pydantic v2：作为内部契约单一事实源，使用 strict model、discriminated union、`extra='forbid'`、`model_json_schema()`。
- `jsonschema`：只用于外部边界或导出 schema 校验，不与 Pydantic 双写业务规则。
- `jsonpointer`：继续作为 artifact edit path，复用现有 `EditArtifact` 的根替换禁止、append 禁止、expected_ref 当前性检查。
- Maildir/spool 思路：大消息先写临时文件，再原子 rename 发布；SQLite 只指向已发布对象。
- Git/CAS 思路：长内容按 sha256 地址化，索引保存 hash 和大小，读取时校验。
- Codex turn stop-condition 思路：模型拥有语义 agenda，代码拥有工具执行、pending input、approval、stop 条件等可判定完成条件。
- Codex 参数修正思路：工具参数错时，不把底层异常当终局，而是把结构化错误作为 observation 回给模型，让模型产出修正后的 typed action 或请求用户补充。
- CQRS 思路：查询走 `FlowQueryService`，命令走 `FlowService`；`/messages` 是协调入口，不直接读写 artifact/flow 表。
- Event Sourcing 思路：message events 是审计流；artifact events 才是业务事实流。

### 拒绝

- 不引入 LangGraph。可借鉴 interrupt/checkpoint/human-in-loop 概念，但它会复制 `artifact_flow` 的职责。
- 不引入 Temporal。可借鉴 signal/update/query、幂等和 durable handler 思想，但 Evo 已有 FlowGate 和 artifact event log。
- 不引入 agent framework、provider registry、第二套 workflow SDK。
- 不在命令入口使用 `json-repair`。结构化输出失败应 fail closed，返回 clarification 或 422。
- 不把旧根目录 `../evo/message_intent` 作为依赖迁移。它只能作为事件/approval/agenda 形状参考；不能 import 旧 runtime adapter/router/service。

## 4. 模块设计

建议新增或保留以下模块，所有模块都在 `evo/message_intent/` 内。命名原则：名字必须暴露“消息层”和“编译到既有 action”的薄边界，避免 `engine`、`workflow`、`executor`、`state_machine` 这类会暗示第二套流程系统的词。

### `schemas.py`

唯一 schema 源。使用 Pydantic v2 strict models。

核心模型：

```python
class TurnPlan(BaseModel):
    schema_version: Literal["message_intent.v1"]
    turn_decision: Literal["next_action", "needs_input", "needs_approval", "final"]
    active_agenda: list[TurnAgendaItem]
    next_action: PlannedAction | None
    user_message_effect: Literal["append", "amend", "replace", "cancel", "none"]
    response_hint: str = ""
```

`PlannedAction` 使用 discriminated union：

- `FlowCommandIntent`
- `ArtifactMutationIntent`
- `FlowQueryIntent`
- `ConfigPatchIntent`
- `ClarificationDraft`
- `TurnFinal`

`FlowCommandIntent.kind`：

- `continue`
- `pause`
- `resume`
- `cancel`
- `retry`

`ArtifactMutationIntent.kind`：

- `edit_artifact`
- `rerun_case_stage`
- `rerun_step`
- `invalidate_from_step`

`FlowQueryIntent.kind`：

- `progress_snapshot`
- `read_step_root`
- `read_case_artifact`

`ConfigPatchIntent.kind`：

- `patch_run_config`
- `patch_source_config`
- `patch_target_config`
- `patch_eval_policy`
- `patch_repair_policy`
- `patch_candidate_config`

所有字段必须是可验证的结构化参数，例如 `step`、`case_id`、`artifact_ref`、`json_pointer`、`value`、`until_step`。禁止模型输出自然语言 command 文本再由代码二次解析。

命名建议：

- `TurnPlan`，不要叫 `WorkflowPlan`。
- `TurnAgendaItem`，不要叫 `TaskState`。
- `PlannedAction`，不要叫 `Operation`。
- `CompiledAction`，不要叫 `ExecutableStep`。
- `ConfigValidationIssue`，不要叫 `RuntimeFailure`。

### 数据契约

第一版只需要以下 DTO，不新增 workflow/state DTO：

```python
class MessageRequest(BaseModel):
    schema_version: Literal["message_request.v1"]
    message_id: str
    text: str
    attachments: list[MessageContentRef] = []
    client_context: dict[str, object] = {}

class MessageTurnResult(BaseModel):
    schema_version: Literal["message_turn_result.v1"]
    thread_id: str
    turn_id: str
    message_id: str
    turn_decision: Literal["needs_input", "needs_approval", "action_executed", "query_answered", "final", "rejected"]
    assistant_text_ref: MessageContentRef | None
    observation_ref: MessageContentRef | None
    pending_input_ref: MessageContentRef | None
    pending_approval_ref: MessageContentRef | None
    action_receipt_ref: MessageContentRef | None
```

`MessageRequest.client_context` 只能放 UI hint，例如当前选中的 case、artifact ref、页面 tab；它不是事实源。所有事实必须通过 `FlowQueryService` 和 artifact effective snapshot 重新读取。

```python
class ConversationContext(BaseModel):
    thread_id: str
    run_id: str
    message_ref: MessageContentRef
    recent_event_summaries: list[dict[str, object]]
    active_agenda_ref: MessageContentRef | None
    pending_input_ref: MessageContentRef | None
    pending_approval_ref: MessageContentRef | None
    flow_snapshot_summary: dict[str, object]
    working_set: dict[str, object] = {}

class CompiledAction(BaseModel):
    action_kind: Literal["flow_command", "evo_query", "artifact_mutation", "need_input", "need_approval", "final"]
    command_id: str = ""
    action_hash: str
    action_ref: MessageContentRef
    approval_required: bool
    base_observation_hash: str
```

`CompiledAction.action_ref` 保存序列化后的 `FlowCommand` / `EvoQuery` / `ApplyArtifactMutation` 参数快照，用于 message 审计和 replay 检查。执行前仍必须重新读取当前 observation；`action_ref` 不是 artifact current/effective 事实源。

### `conversation_context.py`

构造模型可见上下文，只做只读 observation。

输入：

- 当前 user message。
- 最近 N 条 message events 的摘要。
- `active_agenda` / `pending_input` / `pending_approval`。
- `FlowQueryService.snapshot(run_id)` 的结构化结果。
- 必要时使用 `FlowQueryService.read(...)` 读取相关 step root 或 case artifact 的摘要。
- 可选 UI working set，例如当前选中的 case/ref，但必须标注为 UI hint，不是事实源。

上下文窗口策略：

- 默认只放最近消息摘要、未完成 agenda、pending 控制项、当前 snapshot、相关 artifact ref/payload 摘要。
- 大 artifact 不直接塞进 prompt；先放 ref、类型、字段路径摘要，必要时用 query 读取局部。
- 每轮执行后重新 observation，不沿用旧 snapshot 驱动下一步。
- 大 observation 写入 blob，prompt 中只放摘要、blob hash、关键字段和 ref。

接口：

```python
def build_conversation_context(
    *,
    thread_id: str,
    run_id: str,
    message: MessageRequest,
    audit: MessageAuditStore,
    blobs: MessageBlobStore,
    runtime: RuntimeGateway,
) -> ConversationContext: ...
```

输出必须包含当前 `FlowQueryService.snapshot(run_id)` 摘要。需要读取具体 artifact 时，只能通过 `runtime.query(...).read(run_id, EvoQuery)`；不能直接读 artifact store 或 `artifact_heads`。

### `intent_planner.py`

唯一职责：调用模型，把自然语言 + context 转成 `TurnPlan`。

硬约束：

- Planner 不包含关键词表、正则 intent router、相似度路由。
- Provider 支持原生 structured outputs/function calling 时，必须使用 schema constrained 输出。
- Provider 只支持文本时，只允许“模型输出 JSON object + Pydantic strict validation”；不做 repair、不做字段猜测、不做规则兜底。
- 结构校验失败返回 `needs_input` / clarification，不生成命令。

接口：

```python
def plan_next_turn(
    *,
    context: ConversationContext,
    schema: type[TurnPlan],
) -> TurnPlan | InputRequest: ...
```

输入是 `ConversationContext`，输出只能是 Pydantic 校验通过的 `TurnPlan`，或结构化 `InputRequest`。Planner 不返回 `FlowCommand` / `EvoMutation` / `EvoQuery`，也不做审批、执行、存储。

### `action_compiler.py`

把通过校验的 draft 编译为现有 dataclass。

职责：

- 校验 `step` 属于当前 `EvoFlowSpec.steps`。
- 校验 `case_id` 属于当前 `EvoFlowSpec.cases`。
- 校验 `ArtifactRef` 结构可读，必要时通过 `FlowQueryService.read` 或 adapter reader 只读验证。
- `edit_artifact` 必须使用 JSON Pointer；不支持 dotted path、自定义 patch path、根替换、append。
- 生成 deterministic `command_id` / `idempotency_key`。
- 对 `ApplyArtifactMutation` 强制 `command_id == mutation.idempotency_key`。
- 对 destructive 或写入动作生成 approval preview，而不是直接执行。
- 对 `ConfigPatchIntent` 先编译成 `EditArtifact` intent，再进入配置校验/反思流程。

编译结果只允许是：

- `FlowCommand`
- `EvoQuery`
- `ApplyArtifactMutation`
- `NeedInput`
- `NeedApproval`
- `Final`

接口：

```python
def compile_planned_action(
    *,
    plan: TurnPlan,
    context: ConversationContext,
    runtime: RuntimeGateway,
) -> CompiledAction | InputRequest | ApprovalRequest | TurnFinal: ...
```

映射规则：

- `FlowCommandIntent(kind="continue")` -> `ContinueFlow(command_id, until_step)`。
- `FlowCommandIntent(kind="pause")` -> `PauseFlow(command_id)`。
- `FlowCommandIntent(kind="resume")` -> `ResumeFlow(command_id)`。
- `FlowCommandIntent(kind="cancel")` -> `CancelFlow(command_id)`。
- `FlowCommandIntent(kind="retry")` -> `RetryFlow(command_id)`。
- `FlowQueryIntent(kind="progress_snapshot")` -> `ReadProgressSnapshot()`。
- `FlowQueryIntent(kind="read_step_root")` -> `ReadStepRoot(step)`。
- `FlowQueryIntent(kind="read_case_artifact")` -> `ReadCaseArtifact(case_id, kind)`。
- `ArtifactMutationIntent(kind="edit_artifact")` -> `ApplyArtifactMutation(command_id, EditArtifact(...))`。
- `ArtifactMutationIntent(kind="rerun_case_stage")` -> `ApplyArtifactMutation(command_id, RerunCaseStage(...))`。
- `ArtifactMutationIntent(kind="rerun_step")` -> `ApplyArtifactMutation(command_id, RerunStep(...))`。
- `ArtifactMutationIntent(kind="invalidate_from_step")` -> `ApplyArtifactMutation(command_id, InvalidateFromStep(...))`。
- `ConfigPatchIntent(...)` -> 先形成 config edit draft，交给 `config_guard`，不能直接生成 mutation。

`command_id` / `idempotency_key` 由 `message_id + agenda_item_id + action_kind + payload_sha256` 稳定生成；`ApplyArtifactMutation.command_id` 必须等于内部 mutation 的 `idempotency_key`。

### `config_guard.py`

负责配置参数草案的本地校验、错误归类和 preview。它不调用模型，不执行 Evo flow，只处理“是否可以把这个 intent 变成安全的 `EditArtifact`”。

输入：

- `ConfigPatchIntent` 或 artifact edit intent。
- 当前 effective config artifact ref。
- 当前 spec/cases/steps/catalog。
- 目标 schema 或 validator。

输出：

- `ConfigCheckOk(normalized_patch, preview)`。
- `ConfigCheckNeedsInput(issues)`。
- `ConfigCheckNeedsReflection(issues, intent_snapshot)`。
- `ConfigCheckRejected(issues)`。

接口：

```python
def validate_config_patch(
    *,
    intent: ConfigPatchIntent | ArtifactMutationIntent,
    context: ConversationContext,
    runtime: RuntimeGateway,
) -> ConfigCheckOk | ConfigCheckNeedsInput | ConfigCheckNeedsReflection | ConfigCheckRejected: ...
```

`ConfigCheckOk.normalized_patch` 仍然不是可执行 mutation；它只是 `action_compiler` 后续生成 `EditArtifact(ref, pointer, value, idempotency_key)` 的安全输入。`config_guard` 不写 message store，不调用模型，不调用外部网络，不调用 `FlowService`。

第一版 validator：

- `run.config`：`thread_id`、`mode`、`num_case`、`inputs` 基本一致性；禁止改 `thread_id`。
- `corpus.source_config`：`kb_id`/`csv_data` 至少一个有效，`target_case_count`/`min_case_count` 为正整数。
- `eval.target_config`：`target_chat_url` 必须是 http(s) chat/stream URL，`llm_config.llm` 必须存在。
- `eval.policy`：`judge_llm_config.evo_llm` 必须存在；阈值必须是有限数。
- `repair.policy`：`llm_config.evo_llm` 必须存在；workspace namespace 不允许跨 thread。
- `abtest.candidate_config`：URL、instance_count、timeout、algorithm_id/code_path 做结构校验；不调用外部 router 做副作用检查。
- `llm_config`：不只检查 mapping；至少检查 role 名、模型名/提供方/地址/API key 引用等必需字段，具体字段以后以 core model-config contract 为准。

校验失败不直接报错给用户；先形成结构化 `ConfigValidationIssue`：

```python
class ConfigValidationIssue(BaseModel):
    path: str
    code: Literal[
        "missing_required",
        "invalid_type",
        "invalid_url",
        "out_of_range",
        "unknown_field",
        "immutable_field",
        "unsafe_secret",
        "cross_thread_reference",
    ]
    message: str
    observed_value_summary: str = ""
```

失败分类：

- `schema_invalid`：未知字段、类型错误、JSON schema 不满足；可进入一次 reflection。
- `semantic_invalid`：case_id、step、partition、json pointer、num_case 不一致；可 reflection 或 needs_input。
- `config_missing`：缺模型 role、URL、API key 引用、KB/CSV；通常 needs_input。
- `external_unavailable`：HTTP/model/router/KB 连接失败；不自动改配置，除非用户提供替代参数。
- `stale_or_conflict`：expected_ref 或 command receipt 冲突；重新 observe + replan。
- `destructive_or_expensive`：rerun/invalidate/repair/abtest/register 服务；必须 approval。
- `internal_bug`：materializer contract、unexpected exception；不让模型猜修，返回 failed observation。

当前主干容错判断：

- 创建 thread 的 `_inputs/_llm_config` 只做浅校验。
- `LazyLLMClient` 是惰性初始化，模型配置错误常在首次 operation 调用才暴露。
- Runtime 会捕获 materializer 异常并返回 failed op；`FlowService` 会把 flow gate 置为 failed，不应导致服务进程整体 down。
- 因此 message_intent 的职责不是替代 runtime 错误处理，而是在提交配置 mutation 之前多一层 typed validation/preview/reflection，并在 failed observation 后帮助用户形成“修配置 -> 审批 -> mutation -> retry -> continue”的 agenda。

Dry-run/preview 规则：

- 只读当前 effective config artifact ref。
- 在内存副本上应用 JSON Pointer patch。
- 运行 Pydantic/jsonschema/local validators。
- 输出 before/after 摘要、validation report、预计影响的 downstream step/key。
- 禁止 dry-run 调 `adapter.tick`、外部 chat、router register、opencode、LLM probe。
- 网络/model probe 如未来需要，必须是单独显式审批 action。

### `reflection_loop.py`

让模型基于 `ConfigValidationIssue` 修正 typed intent，而不是让代码规则化猜测。反思 pass 是有限、可审计、fail-closed 的，不持久化 repair loop 状态，只记录审计事件。

流程：

1. Planner 产出 `ConfigPatchIntent` 或 `ArtifactMutationIntent(edit_artifact)`。
2. `config_guard.validate_config_patch(...)` 返回 issues。
3. 将 issues、原 intent、允许 schema、当前 observation 摘要写入 blob，并给模型一次 `repair_intent` structured output。
4. 模型只能输出：
   - corrected intent。
   - needs_input。
   - reject。
5. 最多 1 次自动反思修正；仍失败则请求用户补充。若后续确实需要第 2 次，必须由测试证明收益。
6. corrected intent 必须重新走 Pydantic + config guard + approval。

重要边界：

- 反思修正不能直接改 artifact。
- 反思修正不能绕过 approval。
- 反思修正不能调用外部服务做真实执行。
- 反思修正不能用规则把错误码映射成修复动作；错误码只是模型输入。
- 反思修正不能修 `internal_bug` 类错误。

接口：

```python
def reflect_config_error(
    *,
    issues: list[ConfigValidationIssue],
    original_intent: ConfigPatchIntent | ArtifactMutationIntent,
    context: ConversationContext,
) -> ConfigPatchIntent | ArtifactMutationIntent | InputRequest | TurnFinal: ...
```

输出若是 corrected intent，必须重新进入 Pydantic validation -> `config_guard` -> approval；不能从 reflection 输出直接执行。

### `message_turn_handler.py`

`POST /threads/{thread_id}/messages` 的 turn handler。它是请求处理器，不是 workflow engine，也不是后台调度器。

职责：

- append `message_received`。
- 获得短事务 thread lease，防止同一 thread 多个 message turn 同时修改 `message_projection`。
- 构造 context。
- 调 planner 得到 `TurnPlan`。
- 调 action compiler 编译一个 action。
- 对配置草案先走 `config_guard`，必要时进入一次 `reflection_loop`。
- 写 `PendingAction` 审计事件，再在同一 request turn 内执行已通过审批/校验的 action。
- 对 query 立即执行。
- 对需要审批的 command/mutation 写入 `pending_approval`。
- 对可执行 command/mutation 调 `FlowService.handle(...)`。
- append observation。
- 判断本轮是否完成并返回用户。

它不负责：

- 自己 tick runtime。
- 自己后台调度 op。
- 自己判断 step completed。
- 直接写 artifact/flow store。
- 后台消费 pending action。
- 自动重试或调度下一轮 action。

每个 message turn 最多提交一个副作用 action。只读 query 可以在当前 turn 内执行；副作用 command/mutation 执行后必须重新 snapshot、写 observation、返回用户，不在 message 层自循环推进 agenda。

接口：

```python
def handle_message_turn(
    *,
    thread_id: str,
    request: MessageRequest,
    audit: MessageAuditStore,
    blobs: MessageBlobStore,
    runtime: RuntimeGateway,
) -> MessageTurnResult: ...
```

输入只接受 HTTP 层已经校验过的 `MessageRequest` 和四个端口对象。输出只返回 `MessageTurnResult`，其中长文本、preview、observation、receipt 都是 blob ref。HTTP 层不直接接触 planner/compiler/FlowService。

### `message_audit_store.py`

只保存 message-turn audit、pending control projection 和 message-level receipt，不保存长正文。它不是独立 SQLite store，只接受主干 `artifact_store.sqlite3` 的 path/connection，不创建、不拥有新的 SQLite 文件。

推荐事务语义：

- `(thread_id, message_id)` 唯一。
- 同 message id + 同 payload replay 原结果。
- 同 message id + 不同 payload conflict。
- pending action 使用 deterministic `action_hash`。
- pending approval 带 `approval_token`、`expires_at`、`compiled_action_ref`、`base_observation_hash`。
- lease 使用同库 SQLite `BEGIN IMMEDIATE` 或 compare-and-swap owner，事务必须短，避免长时间占用 artifact/flow 写锁。
- 每条 event 的长内容必须先进入 `MessageBlobStore`，SQLite 再记录指针。
- 小于阈值的摘要可以内联，但原文仍建议进入 blob，避免 SQLite 行大小不可控。

PendingAction 语义：

- 模型 plan 不能直接执行；先写 `pending_action_recorded` message event/blob。
- `dispatch_pending_action(...)` 只在同一 `/messages` request turn 内同步调用 Flow/Evo public seam。
- 执行结果写 `ActionReceipt` / `message_receipts`，并写 `pending_action_done/failed/conflict` 事件。
- pending action 只解决“本轮已编译 action 的幂等和崩溃审计”，不提供后台 worker、retry scheduler、poller、runtime dispatch loop。

`delete_thread` 时应清理 `message_*` rows 和 blobs，但 message 清理不应影响 artifact store 或 FlowGate。

接口：

```python
def append_event(
    *,
    thread_id: str,
    turn_id: str,
    message_id: str,
    event_kind: str,
    content_ref: MessageContentRef,
    short_text: str = "",
) -> IndexedMessageEvent: ...

def get_projection(thread_id: str) -> MessageProjection: ...

def update_projection(
    *,
    thread_id: str,
    active_agenda_ref: MessageContentRef | None,
    pending_input_ref: MessageContentRef | None,
    pending_approval_ref: MessageContentRef | None,
    last_observation_ref: MessageContentRef | None,
    last_observation_hash: str,
) -> None: ...

def record_receipt(
    *,
    thread_id: str,
    message_id: str,
    agenda_item_id: str,
    action_hash: str,
    command_id: str,
    outcome_kind: str,
    receipt_ref: MessageContentRef,
) -> ActionReceipt: ...
```

这些接口只能写 `message_events/message_turns/message_projection/message_receipts`。禁止提供“按 step 查询状态”“按 artifact ref 查询 current”“按 worker lease 获取任务”等接口。

### `message_blobs.py`

管理 message blob 文件，不参与 Evo artifact runtime。

核心接口：

- `put_json(kind, value) -> MessageContentRef`
- `put_text(kind, text) -> MessageContentRef`
- `get_json(ref) -> object`
- `get_text(ref) -> str`
- `verify(ref) -> bool`
- `gc_indexed(index_refs)`

也可以采用更语义化的接口名：

- `append_message_blob(...) -> MessageContentRef`
- `load_message_content(ref) -> bytes | str | object`
- `verify_message_content(ref) -> bool`
- `gc_unindexed_blobs(live_refs)`

`MessageContentRef` 字段：

- `uri`
- `sha256`
- `byte_size`
- `mime_type`
- `compression`
- `created_at`

正式接口收敛为语义化命名：

```python
def append_message_blob(
    *,
    thread_id: str,
    turn_id: str,
    kind: str,
    payload: bytes,
    mime_type: str,
    compression: str = "",
) -> MessageContentRef: ...

def load_message_content(ref: MessageContentRef) -> bytes: ...

def verify_message_content(ref: MessageContentRef) -> bool: ...

def gc_unindexed_blobs(live_refs: set[MessageContentRef]) -> None: ...
```

`put_json/get_json/put_text/get_text` 可以作为内部 helper，但对外接口只暴露 `append_message_blob/load_message_content/verify_message_content/gc_unindexed_blobs`，避免业务层依赖存储格式。

实现要求：

- 路径必须在 `message-store/blobs` 下，防 path traversal。
- JSON 序列化使用 canonical JSON。
- 大文本可 gzip，但 hash 应基于存储后的 bytes；content encoding 记录在 index。
- 不存 secret 原值到 prompt 摘要；blob 可以保存原始审计，但 projection/API 默认 redaction。

SQLite 中短摘要限制：

- `short_text` / `summary_json` 建议限制 1-4KB。
- 完整 prompt/context/plan/observation/assistant response 必须文件化。
- `pending_approval` 可以内联 token/action kind/base hash；如果 preview 或 `EditArtifact.value` 很大，必须使用 blob 指针。

### `runtime_gateway.py`

薄端口，只暴露当前主干 public seams：

- `flow(num_case).handle(run_id, command)`
- `query(num_case).snapshot/progress/read`
- `spec(num_case)`
- 需要时读取 `run_config` 取得 `num_case`

接口：

```python
def resolve_run(thread_id: str) -> RunBinding: ...

class RunBinding(BaseModel):
    thread_id: str
    run_id: str
    num_case: int
    mode: str

def flow(num_case: int) -> FlowService: ...

def query(num_case: int) -> FlowQueryService: ...

def spec(num_case: int) -> EvoFlowSpec: ...
```

`resolve_run` 只做 thread -> run/num_case 的服务层绑定，不读写 artifact facts。`flow/query/spec` 返回主干对象，message 层只能调用它们的 public methods。

禁止暴露：

- `adapter.tick`
- `store.commit_external`
- `store.invalidate`
- `operations`
- raw SQLite connection for artifact/flow tables

### 推荐命名汇总

文件名：

- `schemas.py`
- `message_blobs.py`
- `message_audit_store.py`
- `conversation_context.py`
- `intent_planner.py`
- `action_compiler.py`
- `config_guard.py`
- `reflection_loop.py`
- `runtime_gateway.py`
- `message_turn_handler.py`

类名：

- `TurnPlan`
- `TurnAgendaItem`
- `PlannedAction`
- `FlowCommandIntent`
- `ArtifactMutationIntent`
- `FlowQueryIntent`
- `ConfigPatchIntent`
- `InputRequest`
- `ApprovalRequest`
- `TurnFinal`
- `MessageEnvelope`
- `MessageContentRef`
- `IndexedMessageEvent`
- `PendingAction`
- `ActionReceipt`
- `MessageBlobStore`
- `MessageAuditStore`
- `RuntimeGateway`
- `ConfigGuard`
- `ConfigValidationIssue`
- `ConfigReflectionRequest`
- `ConfigRepairPlan`
- `ConfigReflectionPass`
- `MessageTurnHandler`

函数名：

- `append_message_blob`
- `index_message_event`
- `load_message_content`
- `build_conversation_context`
- `plan_next_turn`
- `validate_turn_plan`
- `compile_planned_action`
- `record_pending_action`
- `dispatch_pending_action`
- `record_action_receipt`
- `reobserve_after_action`
- `validate_config_patch`
- `reflect_config_error`
- `propose_config_patch`
- `apply_approved_config_patch`
- `is_turn_complete`

避免命名：

- `WorkflowState`
- `RuntimeState`
- `TaskStatus`
- `OperationExecutor`
- `IntentRouter`
- `RuleParser`
- `StateMachine`

## 5. 单轮请求完成条件

### 单轮流程

1. Receive：写入 `message_received`。
2. Observe：读 `FlowQueryService.snapshot`，加载 active/pending/message 摘要。
3. Plan：模型输出一个 `TurnPlan`。
4. Validate：Pydantic strict validation。
5. Build：把 `next_action` 编译为现有 command/query/mutation。
6. Guard：如果是配置或 artifact edit，先校验参数；失败时进入一次 reflection repair 或请求用户补充。
7. Approve or execute：需要审批则停在 pending approval；只读 query 立即执行；可执行 command/mutation 走 `FlowService.handle`。
8. Observe again：写入 command result 和最新 snapshot 摘要，长 observation 写 blob，index 写 hash。
9. Return：返回 assistant 文案、pending 控制项和最新 observation 摘要。副作用 action 已执行过一次时，本轮必须结束；下一步由下一条用户消息或显式 continue message 触发。

### Turn complete 条件

message turn 结束不是“Evo run 完成”，而是本次 `/messages` 请求处理完成：

- 没有正在执行的 FlowCommand。
- 没有待处理 tool/query/mutation 结果。
- 没有 pending user input。
- 没有 pending approval。
- active agenda 为空，或模型明确输出 `turn_decision="final"`。
- 最近 command outcome 没有要求重新 observation + replan。
- 最近 config guard 没有未解决 issue。
- 如果 flow 处于 failed/cancelled/paused，必须把该状态作为 observation 告知用户，而不是继续猜下一步。

这个迁移自 Codex 的设计思想：模型决定语义 agenda，代码用 pending input、tool follow-up、approval、stop hook 等可判定条件决定本轮是否完成。但这里不迁移 Codex 的完整任务管理，也不在 message 层维护新的流程状态。

硬限制：

- 一个 message turn 最多执行一个副作用 `FlowCommand` / `ApplyArtifactMutation`。
- 只读 `EvoQuery` 可以 inline 执行；执行副作用后必须重新 snapshot 并返回用户。
- `active_agenda` 只是可丢弃的语义草稿，不是 durable workflow backlog。
- `active_agenda` 禁止出现 `current_item`、`step_status`、`retry_count`、`due_at`、`locked_by_worker` 等调度字段。

## 6. 多轮补充与修正

### 用户在等待补充时追加

- 新消息 append 到 message events。
- Planner 输入包含原 `pending_input` 和 `active_agenda`。
- 模型输出重写后的 `TurnPlan`。
- 原 pending_input 标为 resolved/superseded。

### 用户在 pending approval 时追加

模型必须在结构化输出里明确：

- approve：执行原 compiled action。
- reject/cancel：关闭 pending approval。
- amend/replace：关闭原 pending approval，基于新消息和当前 observation 重新 plan。
- unclear：继续 pending approval 并请求 clarification。

代码不能用“好的/确认/取消”等关键词解析 approval；这些仍然由模型结构化输出决定。

### 用户在配置修正时追加

- 新消息与 `ConfigValidationIssue` 一起进入 planner/reflection context。
- 如果用户提供了缺失参数，模型输出 corrected `ConfigPatchIntent`。
- 如果用户否认原修改，模型输出 cancel/supersede。
- 如果用户给了另一套配置，模型输出 replace，而不是叠加旧 patch。
- corrected intent 仍必须重新校验和审批。

### 用户在执行单个任务时追加

- 新消息先作为 amendment event 入队。
- 如果新消息要求改变正在执行的副作用动作，Planner 可以输出 `pause` 或等待当前 command 返回 receipt 后 replan；代码不能凭关键词自动 pause。
- 下一次可执行前必须重新 observation，旧 artifact ref 可能已经 stale。
- 如果将要执行 artifact edit，`EditArtifact` 的 `expected_ref` 保护 stale；stale 结果进入 re-observe + replan，不直接重试写入。

### agenda 重写规则

- `active_agenda` 是模型维护的语义投影，不是业务状态。
- 新消息可 `append`、`amend`、`replace`、`cancel`。
- 被修正的 agenda item 标为 `superseded`，不并发执行冲突动作。
- 执行顺序默认按模型输出的 agenda item order；代码只负责一次执行一个 action 并重新 observation。

## 7. 审批策略

第一版建议保守：

默认无需审批：

- `EvoQuery`
- `PauseFlow`
- `ReadProgressSnapshot`

默认需要审批：

- `EditArtifact`
- `RerunCaseStage`
- `RerunStep`
- `InvalidateFromStep`
- `CancelFlow`
- 自动 `ContinueFlow` 到较远 step，特别是会跨 checkpoint 或触发大量执行时

可配置但不要过度抽象：

- 创建 thread 时若 mode 是 `auto`，可以允许 start/continue 到 final。
- interactive mode 默认 checkpoint 后需要用户确认。

审批 preview 必须显示：

- action kind。
- 目标 step/case/ref/pointer。
- base observation hash 或 ref。
- 预期影响说明。
- deterministic command id。
- 参数校验摘要。
- 如果经过 reflection repair，显示原 draft 与 corrected draft 的差异摘要。

审批通过前不写 artifact、不发 FlowCommand。

## 8. 幂等与并发

### command id

稳定生成：

```text
msgi:<thread_id>:<message_id>:<agenda_item_id>:<action_kind>:<payload_sha256>
```

规则：

- 同 message + 同 action replay 同 receipt。
- 同 message id + 不同 payload 409 conflict。
- `ApplyArtifactMutation.command_id` 必须等于 mutation `idempotency_key`。
- `ContinueFlow` / `PauseFlow` / `ResumeFlow` / `CancelFlow` / `RetryFlow` 使用同样 deterministic command id。

### 并发

- 同 thread 同时只允许一个 message turn 修改 `message_projection`。
- Flow 执行本身仍交给 `FlowService` 和现有 active command 保护。
- `PauseFlow` 可以在执行中作为 gate command 进入 FlowService；但“是否要 pause”来自模型结构化 action 或明确 UI action，不来自关键词规则。
- 多进程场景不能只依赖 `ThreadService._active`；message store lease/receipt 必须用 SQLite 事务保证 replay/conflict。

### 文件 blob 幂等

- blob id 使用 sha256；相同 bytes 可以共享一个 blob。
- index event id 不等于 blob id；同一 blob 可以被多个 event 引用。
- index 写入必须带 `blob_sha256` 和 `action_hash`，避免同 message id 不同内容误 replay。
- GC 只清理未被 index 引用且超过保留窗口的 blob。

## 9. 能力覆盖

第一版只覆盖主干已经有的 public action，不新增业务 operation。

### `/messages` 内部步骤映射

| Message 步骤 | 输入 | 输出 | 可调用主干接口 | 禁止事项 |
| --- | --- | --- | --- | --- |
| Receive | `thread_id`, `MessageRequest` | `message_received` event + message blob ref | `MessageAuditStore.append_event`, `MessageBlobStore.append_message_blob` | 不解析 intent，不调用 Flow |
| Observe | message blob ref, projection | `ConversationContext` | `FlowQueryService.snapshot`, 必要时 `FlowQueryService.read` | 不读 raw SQLite artifact/flow 表 |
| Plan | `ConversationContext` | `TurnPlan` 或 `InputRequest` | structured model port | 不做关键词/正则/相似度路由 |
| Validate | `TurnPlan` | typed plan 或 `InputRequest` | Pydantic strict validation | 不 repair JSON，不猜字段 |
| Compile | typed plan | `CompiledAction` / `ApprovalRequest` / `TurnFinal` | `EvoFlowSpec`, `FlowCommand`, `EvoQuery`, `ApplyArtifactMutation` dataclass | 不执行 action，不写 artifact |
| Guard | config/edit draft | `ConfigCheck*` | Pydantic/jsonschema/jsonpointer/local validators | 不调模型，不调外部服务 |
| Reflect | `ConfigValidationIssue` + original intent | corrected intent / `InputRequest` / reject | structured model port | 最多一次，不绕过 approval |
| Approve | `ApprovalRequest` | `pending_approval_ref` 或 approved compiled action | `MessageAuditStore.update_projection` | 不靠关键词判断确认/取消 |
| Dispatch query | `CompiledAction(EvoQuery)` | query result blob | `FlowQueryService.read/snapshot/progress` | 不写 FlowGate/artifact |
| Dispatch command | `CompiledAction(FlowCommand)` | command receipt blob | `FlowService.handle(run_id, command)` | 不直接 tick runtime |
| Dispatch mutation | `CompiledAction(ApplyArtifactMutation)` | command receipt blob | `FlowService.handle(run_id, ApplyArtifactMutation)` | 不直接 `commit_external/invalidate` |
| Re-observe | receipt | latest snapshot blob | `FlowQueryService.snapshot` | 不继续自循环推进 agenda |
| Return | refs/projection/receipt | `MessageTurnResult` | message DTO | 不返回无限长 inline payload |

### Evo 步骤和 artifact 映射

当前主干步骤固定来自 `EvoFlowSpec.steps`：

| step | root artifact | `ReadStepRoot(step)` 读取 | `RerunStep(step)` / `InvalidateFromStep(step)` 影响 |
| --- | --- | --- | --- |
| `dataset` | `eval.dataset` | 当前 effective dataset root | dataset 输出；invalidate from dataset 会影响后续全部 step |
| `eval` | `eval.summary` | 当前 effective eval summary | eval 输出；invalidate from eval 会影响 eval/analysis/repair/abtest |
| `analysis` | `analysis.summary` | 当前 effective analysis summary | analysis 输出；invalidate from analysis 会影响 analysis/repair/abtest |
| `repair` | `repair.verified_patch` | 当前 effective repair patch | repair 输出；invalidate from repair 会影响 repair/abtest |
| `abtest` | `abtest.comparison` | 当前 effective abtest comparison | abtest 输出 |

注意：图里声明了 `repair/abtest`，但是否真实可跑取决于 materializer 是否注册成功。`message_intent` 不根据 step 名猜测可运行性；它只通过最新 snapshot/receipt 把 failed observation 返回给用户，必要时引导配置修正或停止。

### 用户指令到主干动作映射

| 用户意图例子 | 模型结构化输出 | 编译结果 | 是否默认审批 | 执行/读取接口 |
| --- | --- | --- | --- | --- |
| “现在跑到哪了？” | `FlowQueryIntent(progress_snapshot)` | `ReadProgressSnapshot()` | 否 | `FlowQueryService.snapshot/progress` |
| “看 dataset 的结果” | `FlowQueryIntent(read_step_root, step="dataset")` | `ReadStepRoot("dataset")` | 否 | `FlowQueryService.read` |
| “看 case_0003 的评测答案” | `FlowQueryIntent(read_case_artifact, case_id="case_0003", kind="eval_answer")` | `ReadCaseArtifact("case_0003", "eval_answer")` | 否 | `FlowQueryService.read` |
| “继续跑” | `FlowCommandIntent(continue, until_step="")` | `ContinueFlow(command_id, "")` | interactive 可需要 | `FlowService.handle` |
| “跑到 analysis 停下” | `FlowCommandIntent(continue, until_step="analysis")` | `ContinueFlow(command_id, "analysis")` | 是，若跨 checkpoint/大执行 | `FlowService.handle` |
| “暂停/恢复/取消/重试” | `FlowCommandIntent(pause/resume/cancel/retry)` | 对应 `PauseFlow/ResumeFlow/CancelFlow/RetryFlow` | cancel 默认是 | `FlowService.handle` |
| “把 target_chat_url 改成 ...” | `ConfigPatchIntent(patch_target_config, pointer="/target_chat_url", value=...)` | guard 后 `ApplyArtifactMutation(EditArtifact(...))` | 是 | `FlowService.handle` |
| “num_case 改成 20” | `ConfigPatchIntent(patch_run_config, pointer="/num_case", value=20)` | guard 后 `EditArtifact`；若当前语义不支持变更则 reject/needs_input | 是 | `FlowService.handle` |
| “重跑 case_0003 的 eval” | `ArtifactMutationIntent(rerun_case_stage, case_id="case_0003", stage="eval")` | `ApplyArtifactMutation(RerunCaseStage(...))` | 是 | `FlowService.handle` |
| “重新生成 eval” | `ArtifactMutationIntent(rerun_step, step="eval")` | `ApplyArtifactMutation(RerunStep("eval", ...))` | 是 | `FlowService.handle` |
| “从 analysis 开始作废重跑” | `ArtifactMutationIntent(invalidate_from_step, step="analysis")` | `ApplyArtifactMutation(InvalidateFromStep("analysis", ...))` | 是 | `FlowService.handle` |
| “不是 case_0001，是 case_0003” | `user_message_effect="replace"` + new plan | supersede old pending item, compile new action | 继承新 action 策略 | 重新 observe 后再 compile |
| “确认执行/取消刚才那个” | structured `approve` / `reject` decision | approved compiled action / close pending approval | approve 不再二次审批 | approval 决策仍由模型结构化输出 |

### case artifact kind 映射

`ReadCaseArtifact(case_id, kind)` 的 `kind` 只能来自 `EvoFlowSpec.read_case_artifact` 支持的集合：

| kind | artifact |
| --- | --- |
| `dataset_case` | `eval.case[case_id]` |
| `eval_answer` | `eval.rag_answer[case_id]` |
| `eval_judge` | `eval.judge_result[case_id]` |
| `analysis_trace` | `analysis.trace_summary[case_id]` |
| `analysis_classification` | `analysis.case_classification[case_id]` |
| `abtest_answer` | `abtest.candidate_rag_answer[case_id]` |
| `abtest_judge` | `abtest.candidate_judge_result[case_id]` |

### rerun case stage 映射

`RerunCaseStage(case_id, stage)` 的 `stage` 只能来自 `EvoFlowSpec.rerun_case_stage` 支持的集合：

| stage | invalidated case artifacts |
| --- | --- |
| `dataset` | `eval.case_preparation[case_id]`, `eval.case[case_id]` |
| `eval` | `eval.rag_answer[case_id]`, `eval.judge_result[case_id]` |
| `analysis` | `analysis.trace_summary[case_id]`, `analysis.case_classification[case_id]` |
| `abtest` | `abtest.candidate_rag_answer[case_id]`, `abtest.candidate_judge_result[case_id]` |

### 查询类

- 查进度：`ReadProgressSnapshot` 或 `FlowQueryService.snapshot`。
- 查 step root：`ReadStepRoot(step)`。
- 查 case artifact：`ReadCaseArtifact(case_id, kind)`。

### 流程类

- 继续：`ContinueFlow(command_id, until_step)`。
- 暂停：`PauseFlow(command_id)`。
- 恢复：`ResumeFlow(command_id)`。
- 取消：`CancelFlow(command_id)`。
- 重试失败：`RetryFlow(command_id)`，必要时后接 `ContinueFlow`，但仍是两个显式 command。

### 变更类

- 修改 artifact 字段：`ApplyArtifactMutation(EditArtifact(ref, pointer, value, idempotency_key))`。
- 重跑某 case stage：`ApplyArtifactMutation(RerunCaseStage(case_id, stage, idempotency_key))`。
- 重跑某 step：`ApplyArtifactMutation(RerunStep(step, idempotency_key))`。
- 回退到某 step：`ApplyArtifactMutation(InvalidateFromStep(step, idempotency_key))`。
- 配置修改：先 `ConfigPatchIntent`，经 `config_guard` 和 approval 后编译为 `ApplyArtifactMutation(EditArtifact(...))`。

### 明确缺口

- 当前没有“创建 thread 后追加 seed 输入”的通用 mutation。
- `EditArtifact` 不支持根替换，不支持 JSON Pointer `-` append。
- 不承诺任何现有 actions 之外的“业务智能修复”，除非先迁移成 artifact operation 或 EvoMutation。
- 配置 guard 只做结构/本地安全校验；不会为了验证参数而调用外部 RAG、router、LLM 或运行 operation。

## 10. 接入边界

最小代码落点：

- `evo/message_intent/schemas.py`
- `evo/message_intent/message_blobs.py`
- `evo/message_intent/message_audit_store.py`
- `evo/message_intent/conversation_context.py`
- `evo/message_intent/intent_planner.py`
- `evo/message_intent/action_compiler.py`
- `evo/message_intent/config_guard.py`
- `evo/message_intent/reflection_loop.py`
- `evo/message_intent/runtime_gateway.py`
- `evo/message_intent/message_turn_handler.py`
- `evo/service/api.py`：把 501 `/threads/{thread_id}/messages` 改成调用很薄的 `MessageTurnHandler`。
- `evo/service/threads.py`：删除 thread 时清理 `message_*` rows 和 blob；不改 flow/runtime 逻辑。

禁止改动：

- `evo/artifact_runtime/kernel/*`
- `evo/artifact_runtime/evo/use_cases.py` 的核心语义，除非新增正式 EvoAction 且有独立评审。
- `evo/artifact_flow/service.py` 的执行语义。
- `evo/operations/*`

## 11. 第三方依赖结论

当前 `evo/requirements.txt` 已有足够依赖：

- `pydantic>=2,<3`：内部 schema、strict validation、JSON Schema 生成。
- `jsonschema>=4.20`：外部 schema 验证。
- `jsonpointer>=3.0`：artifact edit path。
- `json-repair>=0.30`：operations 现存依赖，禁止在 message intent 命令入口 import/use。
- `rapidfuzz`：operations 现存依赖，禁止在 message intent intent 解析中 import/use。
- 标准库 `sqlite3`、`hashlib`、`pathlib`、`tempfile`、`os.replace` 足够实现共库 `message_*` index + 文件化 blob store。

暂不新增：

- `langgraph`
- `temporalio`
- `instructor`
- `outlines`
- `guidance`
- agent/workflow framework
- 专门的 object-store SDK，除非未来要把 blob 放到 S3/OSS；第一版本地文件足够。
- 新的 SQLite 数据库文件；message 表必须复用主干 `artifact_store.sqlite3`。

如果部署需要 OpenAI 原生 structured outputs，可新增一个窄的 optional adapter：

- `StructuredPlannerPort.plan(schema, messages) -> dict`
- OpenAI adapter 使用 strict schema/function calling。
- LazyLLM adapter 若无 schema-native 能力，只能 strict JSON parse + Pydantic validate；失败即 clarification。

不要建立 provider registry 或多层 fallback。

## 12. 测试与验收标准

### 单元测试

- Pydantic schema 拒绝 unknown fields。
- Planner validation 失败不生成 command。
- 编译每一种 draft 到对应 `FlowCommand` / `EvoMutation` / `EvoQuery`。
- `ApplyArtifactMutation.command_id == idempotency_key`。
- JSON Pointer 非法路径、根替换、append 被拒绝或由现有 use case 拒绝。
- 同 message id 同 payload replay；同 message id 不同 payload conflict。
- pending approval approve/reject/amend 都可恢复一致 projection。
- stale `EditArtifact` 结果进入 re-observe，不盲重试。
- BlobStore 原子写、hash 校验、index 指针读取、未索引 blob GC。
- 共库 `message_*` index 不存超长原文，只存 blob 指针和摘要。
- `MessageAuditStore` 只创建 allow-list 内的 `message_events/message_turns/message_projection/message_receipts`，不创建 `message_index.sqlite3`。
- `ConfigPatchIntent` 缺必填字段时不生成 mutation，进入 reflection 或 needs_input。
- reflection 最多一次，修正后仍失败则 clarification。
- immutable 字段如 `run.config.thread_id` 被拒绝。
- `target_chat_url`、`llm_config.llm`、`judge_llm_config.evo_llm` 错误被归类为 `ConfigValidationIssue`。

### 集成测试

- `/messages` 查询进度只走 `FlowQueryService`。
- `/messages` continue 只走 `FlowService.handle(ContinueFlow)`。
- `/messages` artifact edit 先 pending approval，再经 `ApplyArtifactMutation`。
- `/messages` 配置修改先 parameter guard，再 reflection/approval，再经 `ApplyArtifactMutation(EditArtifact)`。
- 一个 message turn 最多执行一个副作用 action；执行后重新 snapshot 并返回用户。
- 多轮修正：先请求 case_0001，追加“不是，改 case_0003”，只执行 case_0003。
- 执行中追加修正：记录 amendment，下一轮 re-observe + replan，不并发写两个冲突 mutation。
- 大消息、长模型输出、长 observation 被写入文件 blob，API 返回 cursor/hash/摘要。
- 删除 thread 时 `message_*` rows/blob 被清理，不影响 artifact store payloads。

### 静态检查

`evo/message_intent` 内不应出现：

- `adapter.tick`
- `graph.next_ops`
- `commit_external`
- `invalidate(`，除非是在测试 mock 或文档中
- `from evo.operations`
- intent parser 中的 `re.compile` / keyword table / fuzzy matching
- `json_repair` 或 `repair_json`
- `rapidfuzz`
- `WorkflowState`
- `StateMachine`
- `OperationExecutor`
- 共库 `message_*` 表中保存 `content_json`、`raw_message` 这类无限长字段；应使用 blob pointer。

### 成功标准

- message_intent 只增加 `/messages` 能力，不改变主干 artifact/runtime/flow 语义。
- 所有用户指令解析都由模型结构化输出完成。
- 所有副作用都经现有 Flow/Evo action。
- 所有业务完成度都从 current snapshot/effective artifacts 推导。
- message projection 删除后，不影响 Evo run 的真实流程和 artifact 事实。
- message audit 损坏最多影响对话审计和 UI 回放，不影响 artifact store。
- 配置错误先被结构化 guard/reflection 捕获；无法自动修正时请求用户补充，而不是把底层异常直接暴露成最终答复。

## 13. 参考资料

- OpenAI Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- OpenAI Function Calling: https://developers.openai.com/api/docs/guides/function-calling
- Pydantic v2 strict mode: https://docs.pydantic.dev/latest/concepts/strict_mode/
- Pydantic JSON Schema: https://docs.pydantic.dev/latest/concepts/json_schema/
- jsonschema validation: https://python-jsonschema.readthedocs.io/en/stable/validate/
- jsonpointer: https://python-json-pointer.readthedocs.io/en/latest/
- SQLite internal vs external BLOBs: https://www.sqlite.org/intern-v-extern-blob.html
- Python Mailbox/Maildir: https://docs.python.org/3/library/mailbox.html
- Git Objects: https://git-scm.com/book/en/v2/Git-Internals-Git-Objects
- LangGraph interrupts / human-in-loop: https://docs.langchain.com/oss/python/langgraph/interrupts
- Temporal workflow messages: https://docs.temporal.io/develop/python/workflows/message-passing
- Azure Event Sourcing pattern: https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing
- Azure CQRS pattern: https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs
