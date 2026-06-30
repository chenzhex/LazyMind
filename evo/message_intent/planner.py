from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from evo.llm import LazyLLMClient

from .schemas import TurnPlan


def plan_next_turn(context: Mapping[str, Any], llm_config: Mapping[str, Any]) -> TurnPlan:
    prompt = (
        'You translate one user message into a strict Evo message_intent TurnPlan. '
        'Return only one JSON object. Do not explain. Do not use markdown. '
        'Allowed next_action.kind: flow, query, mutation, config_patch, approval, clarify, final. '
        'For flow/query/mutation/config_patch, set turn_decision to next_action. '
        'Use needs_approval only when deciding an existing pending approval. '
        'Allowed flow command: continue, pause, resume, cancel, retry. '
        'Allowed query: progress_snapshot, read_step_root, read_case_artifact. '
        'Allowed mutation: edit_artifact, rerun_case_stage, rerun_step, invalidate_from_step. '
        'Allowed config_patch target: run_config, source_config, target_config, eval_policy, '
        'repair_policy, candidate_config. '
        'Use schema_version "message_intent.v1". '
        f'TurnPlan JSON schema: {json.dumps(TurnPlan.model_json_schema(), ensure_ascii=False)}\n'
        f'Context: {json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)}'
    )
    llm = LazyLLMClient(llm_config=llm_config, model='evo_llm')
    try:
        raw = llm(prompt, response_format={'type': 'json_object'})
    except TypeError:
        raw = llm(prompt)
    data = raw if isinstance(raw, Mapping) else json.loads(str(raw))
    return TurnPlan.model_validate(data)
