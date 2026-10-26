from typing import Protocol

from app.services.change_control import ChangeControlStore
from app.tools.base import TimeWindow
from app.tools.models import DeployRecord
from app.tools.scenarios import DEFAULT_SCENARIOS, Scenario, scenario_for


class DeployHistoryTool(Protocol):
    def recent(self, service: str, window: TimeWindow) -> list[DeployRecord]:
        """Deploys of this service inside the window, oldest first."""
        ...


class MockDeployHistoryTool:
    def __init__(self, scenarios: tuple[Scenario, ...] | list[Scenario] = DEFAULT_SCENARIOS) -> None:
        self.scenarios = tuple(scenarios)

    def recent(self, service: str, window: TimeWindow) -> list[DeployRecord]:
        scenario = scenario_for(service, self.scenarios)
        if scenario is None:
            return []
        return sorted(
            (item for item in scenario.deploys if window.contains(item.deployed_at)),
            key=lambda item: item.deployed_at,
        )


class ChangeStoreDeployHistory:
    """Deploy history derived from this system's own promoted changes."""

    _DEPLOYED_STATES = {"promoted_to_prod", "monitoring", "resolved", "rolled_back"}

    def __init__(self, change_store: ChangeControlStore) -> None:
        self.change_store = change_store

    def recent(self, service: str, window: TimeWindow) -> list[DeployRecord]:
        records = []
        for change in self.change_store.list_changes():
            if change.service != service or change.deployment_state not in self._DEPLOYED_STATES:
                continue
            if change.promoted_at is None or not window.contains(change.promoted_at):
                continue
            records.append(
                DeployRecord(
                    service=service,
                    ref=change.commit_sha or change.pr_branch or change.change_id,
                    deployed_at=change.promoted_at,
                    deployed_by=change.promoted_by or "",
                    kind="rollback" if change.deployment_state == "rolled_back" else "code",
                    change_id=change.change_id,
                    status="SUCCESS",
                    summary=change.summary,
                )
            )
        return sorted(records, key=lambda item: item.deployed_at)
