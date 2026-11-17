from pathlib import Path
from typing import Callable

from app.adapters.jenkins_client import JenkinsClient
from app.config import Settings
from app.models.schemas import IncidentRecord
from app.services.change_control import ChangeControlStore
from app.services.integration_factory import raise_unless_mock_fallback
from app.tools.deploy_target import JenkinsDeployTarget
from app.tools.dev_environment import MockDevEnvironment
from app.tools.deploys import ChangeStoreDeployHistory, MockDeployHistoryTool
from app.tools.instrumentation import Toolkit
from app.tools.logs import FileLogsTool, MockLogsTool
from app.tools.memory import LexicalIncidentMemory
from app.tools.metrics import MockMetricsTool, PrometheusMetricsTool
from app.tools.sandbox import GitWorktreeProvider
from app.tools.test_runners import ContainerTestRunner


def build_memory(settings: Settings, incident_records: Callable[[], list[IncidentRecord]]):
    """The incident memory, and the indexer that feeds it (None for lexical memory, which reads the records)."""
    if settings.memory_mode != "hybrid":
        return LexicalIncidentMemory(incident_records), None, "lexical"
    import os

    from app.services.memory_indexer import MemoryIndexer
    from app.tools.document_store import PostgresDocumentStore
    from app.tools.embeddings import HashingEmbedder, OpenAICompatibleEmbedder
    from app.tools.hybrid_memory import HybridIncidentMemory

    if settings.embedding_mode == "hashing":
        embedder = HashingEmbedder(settings.embedding_dimensions)  # offline and not semantic: for demos and tests
    else:
        embedder = OpenAICompatibleEmbedder(
            settings.embedding_base_url,
            os.environ.get(settings.embedding_api_key_env, ""),
            settings.embedding_model,
            settings.embedding_dimensions,
        )
    store = PostgresDocumentStore(settings.database_url, settings.embedding_dimensions)
    store.indexed_incidents(embedder.model)  # fail closed at startup: no database or no pgvector extension
    memory = HybridIncidentMemory(store, embedder, floor=settings.memory_semantic_floor)
    return memory, MemoryIndexer(store, embedder), f"hybrid:{embedder.model}"


def _mcp(settings: Settings, families: list[str]):
    """Connect to the manifest's servers and check they offer the bound tools. Fails closed at startup."""
    from app.config import mcp_manifest_path
    from app.mcp.manager import McpConnectionManager
    from app.mcp.manifest import load_manifest

    manifest = load_manifest(mcp_manifest_path(settings))
    manager = McpConnectionManager(manifest.servers)
    wanted: dict[str, set[str]] = {}
    for family in families:
        binding = manifest.bindings[family]
        wanted.setdefault(binding.server, set()).update(binding.tools.values())
    try:
        manager.check_bindings(wanted)
    except Exception:
        manager.close()
        raise
    return manager, manifest


def build_toolkit(
    settings: Settings,
    change_store: ChangeControlStore,
    incident_records: Callable[[], list[IncidentRecord]],
    jenkins_client: JenkinsClient,
    base_dir: str | Path,
    memory=None,
    memory_mode: str | None = None,
) -> tuple[Toolkit, GitWorktreeProvider, dict[str, str]]:
    """Build the tool layer from settings. Returns (toolkit, sandbox provider, mode per tool).

    `memory` is incident memory built by the caller (with `build_memory`, so it can also keep the indexer).
    """
    modes: dict[str, str] = {}
    mcp_families = [
        family
        for family, mode in (("metrics", settings.metrics_mode), ("logs", settings.logs_mode), ("deploys", settings.deploy_history_mode))
        if mode == "mcp"
    ]
    manager = manifest = None
    if mcp_families:
        from app.mcp.backends import McpDeployHistory, McpLogs, McpMetrics, McpPrometheusMetrics

        try:
            manager, manifest = _mcp(settings, mcp_families)
        except Exception as exc:
            raise_unless_mock_fallback(settings, "MCP backends", exc)
            mcp_families = []  # demo-only fallback (ALLOW_MOCK_FALLBACK): the simulators answer instead

    if "metrics" in mcp_families:
        binding = manifest.bindings["metrics"]
        metrics = (McpPrometheusMetrics if binding.adapter == "prometheus" else McpMetrics)(manager, binding)
        modes["metrics"] = f"mcp:{binding.server}"
    elif settings.metrics_mode == "prometheus":
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

    if "logs" in mcp_families:
        logs, modes["logs"] = McpLogs(manager, manifest.bindings["logs"]), f"mcp:{manifest.bindings['logs'].server}"
    elif settings.logs_mode == "file":
        logs, modes["logs"] = FileLogsTool(Path(base_dir) / settings.logs_file_path), "file"
    else:
        logs, modes["logs"] = MockLogsTool(), "mock"

    if "deploys" in mcp_families:
        deploys = McpDeployHistory(manager, manifest.bindings["deploys"])
        modes["deploys"] = f"mcp:{manifest.bindings['deploys'].server}"
    elif settings.deploy_history_mode == "changes":
        deploys, modes["deploys"] = ChangeStoreDeployHistory(change_store), "changes"
    else:
        deploys, modes["deploys"] = MockDeployHistoryTool(), "mock"

    dev_env = None
    if settings.deploy_target_mode == "jenkins":
        deploy_target, modes["deploy_target"] = JenkinsDeployTarget(jenkins_client), "jenkins"
    else:
        # The simulated dev environment is both the deploy target and its metrics, so deploying a
        # branch changes what the Verifier later measures.
        dev_env = MockDevEnvironment(
            default_effect=settings.mock_dev_effect,
            polls_until_done=settings.mock_jenkins_polls_until_done,
        )
        deploy_target, modes["deploy_target"] = dev_env.deploy_target, "mock"

    if settings.dev_metrics_mode == "prometheus":
        try:
            dev_metrics = PrometheusMetricsTool(
                base_url=settings.dev_metrics_base_url,
                timeout_seconds=settings.metrics_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            )
            modes["dev_metrics"] = "prometheus"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "Dev metrics (Prometheus)", exc)
            dev_metrics, modes["dev_metrics"] = (dev_env or MockDevEnvironment()).metrics, "mock-fallback-startup"
    elif dev_env is not None:
        dev_metrics, modes["dev_metrics"] = dev_env.metrics, "mock"
    else:
        # A real deploy target with simulated dev metrics would measure nothing real.
        raise_unless_mock_fallback(
            settings,
            "Dev metrics",
            ValueError("DEPLOY_TARGET_MODE=jenkins needs DEV_METRICS_MODE=prometheus"),
        )
        dev_metrics, modes["dev_metrics"] = MockDevEnvironment().metrics, "mock-fallback-startup"

    if memory is None:
        memory, _, modes["memory"] = build_memory(settings, incident_records)
    else:
        modes["memory"] = memory_mode or "lexical"
    toolkit = Toolkit(
        metrics=metrics,
        logs=logs,
        deploys=deploys,
        memory=memory,
        deploy_target=deploy_target,
        dev_metrics=dev_metrics,
        max_calls_per_run=settings.tool_max_calls_per_run,
        mcp=manager,
    )

    test_runner = None
    modes["test_execution"] = "host"
    if settings.sandbox_exec == "container":
        test_runner = ContainerTestRunner(
            image=settings.sandbox_image,
            command=settings.sandbox_test_command,
            timeout_seconds=settings.test_evidence_timeout_seconds,
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            pids_limit=settings.sandbox_pids_limit,
        )
        # Fail closed at startup: never quietly run model-written code on the host because Docker is missing.
        test_runner.check_available()
        modes["test_execution"] = "container"

    allowed_paths = [part.strip() for part in settings.code_change_allowed_paths.split(",") if part.strip()]
    sandboxes = GitWorktreeProvider(
        repo_root=base_dir,
        allowed_edit_paths=allowed_paths,
        max_edit_lines=settings.code_change_max_lines,
        test_command=settings.test_evidence_command,
        test_timeout_seconds=settings.test_evidence_timeout_seconds,
        test_runner=test_runner,
    )
    return toolkit, sandboxes, modes
