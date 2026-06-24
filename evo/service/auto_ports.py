from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from evo.auto_agent import ActiveApproval, AutoIntervention


class HubAutoAgentPorts:
    def __init__(self, hub: Any) -> None:
        self.hub = hub

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self.hub.get_thread(thread_id)

    def flow_status(self, thread_id: str) -> dict[str, Any]:
        return self.hub.flow_status(thread_id)

    def artifact(self, thread_id: str, artifact_id: str) -> dict[str, Any] | None:
        try:
            return self.hub.artifact(thread_id, artifact_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise

    def active_approval(self, thread_id: str) -> ActiveApproval | None:
        return self.hub.active_approval(thread_id)

    def start_flow(self, thread_id: str, *, command_id: str) -> dict[str, Any]:
        return self.hub.start(thread_id, {'command_id': command_id})

    def continue_flow(self, thread_id: str, *, command_id: str) -> dict[str, Any]:
        return self.hub.continue_thread(thread_id, {'command_id': command_id})

    def pause_flow(self, thread_id: str, *, command_id: str) -> dict[str, Any]:
        return self.hub.pause(thread_id, command_id=command_id)

    def cancel_flow(self, thread_id: str, *, command_id: str) -> dict[str, Any]:
        return self.hub.cancel(thread_id, command_id=command_id)

    def retry_failed(self, thread_id: str, *, command_id: str) -> dict[str, Any]:
        return self.hub.retry(thread_id, {'command_id': command_id})

    def execute_intervention(
        self,
        thread_id: str,
        *,
        command_id: str,
        intervention: AutoIntervention,
    ) -> dict[str, Any]:
        return self.hub.execute_auto_intervention(
            thread_id,
            intervention.model_dump(mode='json'),
            command_id=command_id,
        )

    def resolve_approval(self, thread_id: str, *, action: str, approval_token: str, command_id: str) -> dict[str, Any]:
        return self.hub.resolve_approval(
            thread_id,
            action=action,
            approval_token=approval_token,
            command_id=command_id,
        )
