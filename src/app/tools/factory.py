from pathlib import Path
from typing import Callable

from app.adapters.jenkins_client import JenkinsClient
from app.config import Settings
from app.models.schemas import IncidentRecord
from app.services.change_control import ChangeControlStore
from app.services.integration_factory import raise_unless_mock_fallback
from app.tools.deploy_target import JenkinsDeployTarget, MockDeployTarget
from app.tools.deploys import ChangeStoreDeployHistory, MockDeployHistoryTool
from app.tools.instrumentation import Toolkit
from app.tools.logs import FileLogsTool, MockLogsTool
from app.tools.memory import LexicalIncidentMemory
from app.tools.metrics import MockMetricsTool, PrometheusMetricsTool
from app.tools.sandbox import GitWorktreeProvider


def build_toolkit(
    settings: Settings,
    change_store: ChangeControlStore,
    incident_records: Callable[[], list[IncidentRecord]],
    jenkins_client: JenkinsClient,
    base_dir: str | Path,
) -> tuple[Toolkit, GitWorktreeProvider, dict[str, str]]:
    """Build the tool layer from settings. Returns (toolkit, sandbox provider, mode per tool)."""
    modes: dict[str, str] = {}

    if settings.metrics_mode == "prometheus":
        try:
            metrics = PrometheusMetricsTool(
                base_url=settings.metrics_base_url,
                timeout_seconds=settings.metrics_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            )
            modes["metrics"] = "prometheus"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "Metrics (Prometheus)", exc)
            metrics, modes["metrics"] = MockMetricsTool(), "mock-fallback-startup"
    else:
        metrics, modes["metrics"] = MockMetricsTool(), "mock"

    if settings.logs_mode == "file":
        logs, modes["logs"] = FileLogsTool(Path(base_dir) / settings.logs_file_path), "file"
    else:
        logs, modes["logs"] = MockLogsTool(), "mock"

    if settings.deploy_history_mode == "changes":
        deploys, modes["deploys"] = ChangeStoreDeployHistory(change_store), "changes"
    else:
        deploys, modes["deploys"] = MockDeployHistoryTool(), "mock"

    if settings.deploy_target_mode == "jenkins":
        deploy_target, modes["deploy_target"] = JenkinsDeployTarget(jenkins_client), "jenkins"
    else:
        deploy_target = MockDeployTarget(
            outcome=settings.mock_jenkins_outcome,
            polls_until_done=settings.mock_jenkins_polls_until_done,
        )
        modes["deploy_target"] = "mock"

    modes["memory"] = "lexical"
    toolkit = Toolkit(
        metrics=metrics,
        logs=logs,
        deploys=deploys,
        memory=LexicalIncidentMemory(incident_records),
        deploy_target=deploy_target,
        max_calls_per_run=settings.tool_max_calls_per_run,
    )

    allowed_paths = [part.strip() for part in settings.code_change_allowed_paths.split(",") if part.strip()]
    sandboxes = GitWorktreeProvider(
        repo_root=base_dir,
        allowed_edit_paths=allowed_paths,
        max_edit_lines=settings.code_change_max_lines,
        test_command=settings.test_evidence_command,
        test_timeout_seconds=settings.test_evidence_timeout_seconds,
    )
    return toolkit, sandboxes, modes
