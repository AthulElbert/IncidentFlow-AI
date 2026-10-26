"""Builds a complete, real agent run: only the model (a script) and the dev environment (a simulator) are fake.

The investigator, verifier and critic are the real ones, the repository is a real temp git repo whose
worktree the agents edit, and the change store, lifecycle rules and audit trail are the real ones.
"""
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from llm_fakes import ScriptedLLM

from app.agents.critic import Critic
from app.agents.fixer import Fixer
from app.agents.graph import AgentRuntime
from app.agents.investigator import Investigator
from app.agents.runner import AgentRunner
from app.agents.verifier import Verifier
from app.audit import JsonlAuditLog
from app.events.inprocess import InProcessBus
from app.replay import event_for
from app.services.change_control import ChangeControlStore, PolicyConfig
from app.tools.deploys import MockDeployHistoryTool
from app.tools.dev_environment import MockDevEnvironment
from app.tools.instrumentation import Toolkit
from app.tools.logs import MockLogsTool
from app.tools.memory import LexicalIncidentMemory
from app.tools.metrics import MockMetricsTool
from app.tools.sandbox import FileSandbox
from app.tools.scenarios import BAD_CONFIG_LATENCY, DEFAULT_SCENARIOS, Scenario, eval_history

CONFIG_PATH = "faulty_project/app_config.json"
BROKEN = json.dumps({"latency_ms": 1800, "error_probability": 0.25, "retries": 2}, indent=2) + "\n"
FIXED = json.dumps({"latency_ms": 120, "error_probability": 0.01, "retries": 2}, indent=2) + "\n"
INVALID = '{"latency_ms": 120,\n'
NO_RETRIES = json.dumps({"latency_ms": 120, "error_probability": 0.01}) + "\n"

BASE_FILES = {
    CONFIG_PATH: BROKEN,
    "config/other.yaml": "pool_size: 10\n",
    "src/handler.py": "def handle():\n    return 'ok'\n",
    "run_tests.py": (
        "import json, sys\n"
        f"cfg = json.load(open('{CONFIG_PATH}'))\n"
        "sys.exit(0 if 'retries' in cfg else 1)\n"
    ),
}


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=root, check=True, capture_output=True,
    )


def edit(path: str, content: str) -> dict:
    return {"action": "tool", "tool": "sandbox", "method": "edit", "args": {"path": path, "content": content}}


def done(summary: str = "Changed the config") -> dict:
    return {"action": "final", "summary": summary, "rationale": "This addresses the cause.", "no_fix": False}


def make_repo(root: Path) -> Path:
    for rel, text in BASE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


class TempRepoProvider:
    """Hands out a disposable sandbox over a real git repo, like GitWorktreeProvider does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.opened: list[str] = []

    def open(self, branch: str) -> FileSandbox:
        self.opened.append(branch)
        return FileSandbox(
            self.root,
            allowed_edit_paths=["faulty_project", "config", "src"],
            max_edit_lines=50,
            test_command=[sys.executable, "run_tests.py"],
            test_timeout_seconds=60,
            disposable=True,
        )


@dataclass
class Harness:
    root: Path
    store: ChangeControlStore
    audit: JsonlAuditLog
    bus: InProcessBus
    env: MockDevEnvironment
    runtime: AgentRuntime
    runner: AgentRunner
    llm: ScriptedLLM
    provider: TempRepoProvider
    change_id: str

    def record(self):
        return self.store.get_change(self.change_id)

    def run(self):
        return self.runner.run_sync(self.change_id, "tester")


def build(tmp_path: Path, replies=(), scenario: Scenario = BAD_CONFIG_LATENCY, effect: str = "resolves",
          max_attempts: int = 3, with_fixer_llm: bool = True, history: bool = True, workers: int = 1,
          dev_has_service: bool = True) -> Harness:
    root = make_repo(tmp_path / "repo")

    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    bus = InProcessBus()
    store_file = tmp_path / "changes.json"
    store_file.write_text("[]", encoding="utf-8")
    store = ChangeControlStore(str(store_file), policy=PolicyConfig(), audit=audit, bus=bus)

    event = event_for(scenario)
    change = store.create_change(
        incident_id=f"INC-{scenario.name}", service=scenario.service, environment=scenario.environment,
        summary=f"{scenario.service} alert", jira_key="SUP-1", jenkins_job_url="u", proposed_actions=[],
        triage_mode_used="heuristic", triage_hypothesis_steps=[], issue_type="unknown", confidence=0.5,
        warning_count=0, jenkins_status="QUEUED", alert=event.model_dump(mode="json"),
    )

    dev_scenarios = DEFAULT_SCENARIOS if dev_has_service else tuple(s for s in DEFAULT_SCENARIOS if s.service != scenario.service)
    env = MockDevEnvironment(dev_scenarios, default_effect=effect)
    toolkit = Toolkit(
        metrics=MockMetricsTool(DEFAULT_SCENARIOS),
        logs=MockLogsTool(DEFAULT_SCENARIOS),
        deploys=MockDeployHistoryTool(DEFAULT_SCENARIOS),
        memory=LexicalIncidentMemory(lambda: eval_history() if history else []),
        deploy_target=env.deploy_target,
        dev_metrics=env.metrics,
    )
    llm = ScriptedLLM(*replies)
    provider = TempRepoProvider(root)
    runtime = AgentRuntime(
        change_store=store,
        toolkit=toolkit,
        sandbox_provider=provider,
        investigator=Investigator(),
        fixer=Fixer(llm if with_fixer_llm else None, editable_paths=["faulty_project", "config", "src"], max_changed_lines=50),
        verifier=Verifier(
            allowed_paths=["faulty_project", "config", "src"], max_changed_lines=50, deploy_wait_seconds=10,
            poll_interval_seconds=1, clock=lambda: scenario.anchor + timedelta(minutes=30), sleep=lambda _s: None,
        ),
        critic=Critic(),
        audit=audit,
        max_attempts=max_attempts,
    )
    return Harness(root, store, audit, bus, env, runtime, AgentRunner(runtime, workers=workers), llm, provider, change.change_id)
