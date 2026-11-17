# Agentic Production Support Platform

Assistive production-support system for incident triage, safe fix orchestration, approval governance, and controlled production promotion.

## What It Does

- Ingests incidents (`/v1/incidents/process` or `/v1/incidents/mock`)
- Classifies issue type and detects recurring patterns
- Finds similar incidents and prior resolution context
- Generates triage + root-cause hypotheses (heuristic or LLM)
- Creates Jira ticket (mock or real)
- Prepares draft PR metadata with test evidence
- Generates issue-aware patch artifact files
- Optionally creates local git branch refs for PR flow
- Executes dev validation via Jenkins + APM evidence
- Enforces human approval and policy gates before prod
- Promotes to production via Jenkins with audit metadata
- Exposes metrics API and dashboard UI
- Supports scheduler polling mode from APM alert sources

## Current Architecture

- API and orchestration: FastAPI
- Domain/services: classifier, triage agent, fix planner, pipeline, change control, PR preparer
- Adapters: Jira, Jenkins, APM, LLM, PR provider
- Storage backend:
  - `json` (default MVP/local)
  - `postgres` (shared persistence)
- Auth/RBAC:
  - `api_key` | `jwt` | `hybrid`
  - roles: `viewer`, `approver`, `release_operator`

## Project Structure

```text
src/app/main.py                         # FastAPI entrypoint + route wiring
src/app/config.py                       # Environment settings loading
src/app/security.py                     # API key / JWT auth + RBAC
src/app/models/schemas.py               # Request/response and domain schemas
src/app/services/pipeline.py            # Incident processing orchestration
src/app/services/change_control.py      # Approval/policy state machine + persistence
src/app/services/triage_agent.py        # Heuristic/LLM triage + hypothesis
src/app/services/pr_preparer.py         # Draft PR prep + test evidence + patch artifact
src/app/services/metrics.py             # Operational metrics summary builder
src/app/adapters/*.py                   # External integration adapters
frontend/dashboard.html                 # Demo dashboard UI
eval/triage_dataset.jsonl               # Labeled evaluation dataset
scripts/evaluate_triage.py              # Offline triage evaluation harness
data/*.json                             # JSON storage files (when STORAGE_BACKEND=json)
docker-compose.yml                      # API + Postgres local demo
```

## Prerequisites

- Python 3.10+
- Optional: Docker Desktop (for compose demo)

## Setup

```powershell
cd C:\Personal_projects\Agentic_AI_Projects
python -m venv .venv
```

If PowerShell blocks activation, either:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

or run without activation:

```powershell
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

## Configuration

1. Copy `.env.example` to `.env`
2. Set keys/modes for your environment

Key settings groups:

- Storage:
  - `STORAGE_BACKEND`: `json` | `postgres`
  - `DATABASE_URL` (required for postgres)
- PR pipeline:
  - `PR_MODE`: `mock` | `github`
  - `PR_REPO_SLUG`, `PR_GITHUB_TOKEN`, `PR_GITHUB_API_BASE_URL`, `PR_BASE_BRANCH`
  - `PR_LOCAL_BRANCH_MODE`: `spec` | `git`
  - `PR_PATCH_OUTPUT_DIR`
- Code change pipeline:
  - `CODE_CHANGE_MODE`: `spec` | `sandbox_git`
  - `CODE_CHANGE_ALLOWED_PATHS`
  - `CODE_CHANGE_MAX_LINES`
  - `CODE_CHANGE_AUTO_COMMIT`
  - `CODE_CHANGE_AUTO_PUSH`
- Test evidence:
  - `TEST_EVIDENCE_MODE`: `mock` | `pytest`
  - `TEST_EVIDENCE_COMMAND`
  - `TEST_EVIDENCE_TIMEOUT_SECONDS`
- Triage:
  - `TRIAGE_MODE`: `heuristic` | `llm`
  - `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- Integrations:
  - `JIRA_MODE`, `JENKINS_MODE`, `APM_MODE`
- Scheduler:
  - `APM_POLL_MODE`: `off` | `poll`
  - `APM_POLL_INTERVAL_SECONDS`
  - `APM_ALERTS_MODE`: `mock` | `http` | `dynatrace`
  - `APM_ALERTS_BASE_URL`, `APM_ALERTS_TIMEOUT_SECONDS`
  - `APM_ALERTS_DYNATRACE_TOKEN` (required for dynatrace mode)
  - `APM_ALERT_QUEUE_FILE`
  - The scheduler is intake-only: it creates incidents and change records. It never approves, deploys or promotes.
- Policy:
  - `MIN_CONFIDENCE_FOR_PROD`
  - `REQUIRE_ZERO_WARNINGS_FOR_PROD`
  - `ALLOWED_JENKINS_STATES_FOR_PROD` (terminal success states only, default `SUCCESS`; `QUEUED`/`RUNNING`/`UNKNOWN` fail startup)
  - `ENFORCE_SEPARATION_OF_DUTIES` (approver cannot also promote)
- Jenkins build tracking:
  - `JENKINS_BUILD_WAIT_SECONDS`, `JENKINS_POLL_INTERVAL_SECONDS`
  - `MOCK_JENKINS_POLLS`, `MOCK_JENKINS_OUTCOME` (simulated builds: `RUNNING` for N polls, then `success`/`failure`)
- Failure behavior:
  - `ALLOW_MOCK_FALLBACK` (default `false`): a `real` integration that cannot start fails startup instead of silently becoming a simulator
- Auth:
  - `AUTH_MODE`: `api_key` | `jwt` | `hybrid`
  - API keys or JWT settings

## Run API

```powershell
$env:PYTHONPATH = "C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

- Swagger: `http://127.0.0.1:8000/docs`
- Dashboard: `http://127.0.0.1:8000/dashboard`

## Key Endpoints

- `GET /health`
- `POST /v1/incidents/mock`
- `POST /v1/incidents/process`
- `POST /v1/apm/mock-alerts` (enqueue alert for scheduler demo)
- `GET /v1/scheduler/status`
- `POST /v1/scheduler/run-once`
- `GET /v1/changes`
- `GET /v1/changes/{change_id}`
- `GET /v1/changes/overview` (one small row per change, newest first, with what each is waiting for; cheap to poll)
- `GET /v1/changes/{change_id}/review` (the case file a person decides from: diagnosis, evidence, verified diff, checks, Critic, policy result, which actions are open and why not, and a timeline)
- `GET /v1/shadow/report` and `POST /v1/changes/{change_id}/shadow-run` (shadow mode)
- `GET /v1/changes/{change_id}/postmortem` (postmortem draft, markdown)
- `GET /v1/llm/status` (which model each task uses, and endpoint health)
- `GET /v1/whoami` (the actor and role a key belongs to)
- `GET /v1/changes/{change_id}/audit` (who did what to this change, oldest first)
- `GET /v1/audit` (most recent audit entries across all changes)
- `GET /v1/metrics/summary`
- `POST /v1/changes/{change_id}/prepare-pr`
- `POST /v1/changes/{change_id}/execute-dev`
- `POST /v1/changes/{change_id}/investigate` (start an agent run; needs `AGENT_RUN_MODE` manual or auto)
- `GET /v1/changes/{change_id}/agent-run`
- `GET /v1/scenarios` and `POST /v1/incidents/scenario/{name}` (simulated incidents)
- `POST /v1/changes/{change_id}/refresh-status` (resolve a dev build or prod deploy that was still running)
- `POST /v1/changes/{change_id}/decision`
- `POST /v1/changes/{change_id}/promote` (reserves the promotion first; only a confirmed Jenkins `SUCCESS` becomes `promoted_to_prod`)

## Typical Workflow

1. Create/process incident
2. Capture `metadata.change_id`
3. Review change record
4. Prepare PR (`prepare-pr`)
5. Validate generated patch file under `PR_PATCH_OUTPUT_DIR`
6. Execute dev validation
7. Approve or reject
8. Promote to production (release operator)

Notes:

- `prepare-pr` generates issue-aware patch templates:
  - `performance_degradation`
  - `application_error`
  - `dependency_failure`
  - `resource_saturation`
  - `unknown` (manual triage template)
- If `PR_LOCAL_BRANCH_MODE=git`, service attempts `git branch <generated-branch>`
- If `CODE_CHANGE_MODE=sandbox_git`, service creates/uses `.agent_worktrees/<branch>` and applies real file edits with path/size guardrails.
- In scheduler mode (`APM_POLL_MODE=poll`), the app continuously polls alerts and applies dedup before processing.

### LLM routing and fallback

Every model call goes through a router. With only the `LLM_*` settings it is one endpoint with retries (`LLM_MAX_RETRIES`). Set `LLM_ROUTING_FILE` (see `ops/llm_routing.example.json`) to give each task its own ordered list of models:

- **Profiles:** `default`, `triage`, `code_fix`, `investigator.reason`, `fixer.step`, `critic.review`. A task without a profile uses `default`.
- **Fallback:** a timeout, 429 or 5xx is retried with backoff, then the next endpoint is tried; an endpoint that keeps failing is skipped for 30 seconds.
- **Escalation:** `escalate_on_retry` can send a second fix attempt to a stronger model.
- **Local-only tasks:** `local_only_profiles` refuses any route that leaves your network.
- **Keys** are named by environment variable in the file, never written in it, and the file must sit outside the paths agents may edit.

`GET /v1/llm/status` shows the routing and each endpoint's health. Token usage and cost of every call are recorded on the run and in the audit log (never the prompt or reply).

### MCP servers as tool backends

Metrics, logs and deploy history can come from MCP servers: set `METRICS_MODE`, `LOGS_MODE` or `DEPLOY_HISTORY_MODE` to `mcp` and describe the servers in `MCP_MANIFEST` (see `ops/mcp_tools.example.json`). Agents still call the same typed tools; MCP is only the transport.

- **Read-only bindings only.** A binding must declare `"effect": "read"`; tools with side effects are never reachable from an agent.
- **Nothing a server says is trusted.** Its tool descriptions are never shown to a model, its replies are parsed into our own types or refused, and text from it is capped.
- **Prometheus:** the `prometheus` adapter works with servers that run PromQL range queries (such as `prometheus-mcp-server`), using the same fixed query catalog as the direct backend, so a model never supplies PromQL.
- **Isolation:** a stdio server starts with only the environment variables you name, in an empty working directory.
- **Fails closed:** the app checks every bound server and tool at startup and will not start without them.

### Tool registry, follow-up evidence and re-diagnosis

Every tool an agent can use is declared once in `src/app/tools/registry.py`: who may call it, whether a model may ask for it, whether it writes, and its argument schema. Permissions and the example requests in prompts are generated from it, and every request a model makes is checked against it before anything runs. A model may only ask for a write inside its own worktree.

- **Follow-up evidence** (`INVESTIGATION_FOLLOW_UPS`, default 0 = off). Before handing an incident to a person, the Investigator can make a few extra calls chosen from the signals the scoring says are missing: metrics it did not query, older or crowded-out log lines, past incidents of the expected kind. It stops as soon as it has enough to proceed. Try `python scripts/replay_investigations.py --scenarios all --follow-ups 3`.
- **Re-diagnosis.** If a fix applied, passed its tests and deployed but the alert metric did not recover, and the second-ranked diagnosis is strong on its own, the next attempt fixes that one instead, and the change records the revised cause.

### Running a fix's tests in a container

The Verifier runs the tests of a worktree the Fixer edited, and those tests execute code the Fixer's model may have written. `SANDBOX_EXEC=container` runs them in a locked-down container: no network, the worktree read-only, a read-only root filesystem, no Linux capabilities, an unprivileged user, memory, CPU and process limits, and none of the host's environment. Build the image once:

```powershell
docker build -f ops/sandbox.Dockerfile -t incidentflow-sandbox:latest .
```

With `SANDBOX_EXEC=container` the app will not start if Docker or the image is missing, and a Docker failure is reported as "tests could not run" (the run escalates), never as failing tests and never by falling back to the host. `FIXER_MODE=llm` with the default `SANDBOX_EXEC=host` is refused unless you set `ALLOW_HOST_EXECUTION=true`.

### Run budgets

One run has limits on the whole: `AGENT_RUN_MAX_SECONDS` (900), `AGENT_RUN_MAX_TOOL_CALLS` (150, shared by all agents and attempts), `AGENT_RUN_MAX_LLM_CALLS` (60), `AGENT_RUN_MAX_TOKENS` (600,000) and `AGENT_RUN_MAX_COST` (0 = no limit). Time and tool calls are checked before each stage; a tool or model call past its limit is refused where it is attempted, so a fix that used the last model call is still verified. Exceeding one escalates the change with the reason ("run budget exceeded: the run used its 900s time limit"); a fix that is already verified and reviewed is not thrown away because packaging ran past the clock. What a run used is recorded on it (`agent_run.budget`). Tokens and cost are counted when the provider reports usage. Out of model budget, the Investigator answers from the evidence and the Critic reviews by its rules.

### Shadow mode

`AGENT_RUN_MODE=shadow` runs the same agents (investigate, fix in a throwaway worktree on `agent-shadow/...`, verify, review) on every new incident, but records the result only as the change's `shadow` report. It cannot move the change's state, set its issue type or confidence, record dev-execution evidence, or open the approval gate, and the normal run endpoint is refused in this mode. The dashboard shows each shadow run as "not in effect". `GET /v1/shadow/report` sets what the agents would have done against what people did (released, rejected or undecided): the diagnosis against the keyword triage, and the files they would have changed against the files a person released. People never see the agents' proposals in shadow mode, so this is a proxy and the report says so. `POST /v1/changes/{id}/shadow-run` runs the agents on an existing change.

### Kafka (optional)

`EVENT_BUS_MODE=kafka` with `KAFKA_BOOTSTRAP_SERVERS` (and `pip install confluent-kafka`) puts events on Kafka. With JSON storage the store publishes to Kafka after each write; with Postgres storage the Postgres outbox stays the store's bus and a relay forwards it to Kafka, so an event is never lost between the database commit and the broker. Consumers (such as the outcome learner) read from Kafka. This is tested against an in-memory stand-in for the client, not a real broker.

## Learning From Outcomes and Postmortems

Incident memory only offers incidents that someone actually resolved. Every incident has an `outcome`:

| Outcome | Meaning | Offered as evidence? |
|---|---|---|
| `historical` | Seed data recorded before outcomes were tracked | Yes |
| `open` | Filed by the pipeline, still being worked | No (it still counts for recurrence detection) |
| `resolved` | The fix was approved and released to production | Yes |
| `rejected` | A person turned the proposed fix down | No |

When a change reaches `promoted_to_prod` or `closed_rejected`, a background consumer on the event bus (plus a sweep at startup, in case events were lost in a restart) updates its incident. A resolved incident's `resolution` is written from the change's facts: what was changed, the dev before/after alert metric, who approved and who released it. No model writes it. Learning the same outcome twice changes nothing.

`GET /v1/changes/{id}/postmortem` returns a markdown postmortem draft built from the same records (summary, diagnosis and the evidence behind it, the diff, verification, decisions, timeline) with a checklist of judgement calls left to the people involved. The dashboard shows it for finished changes. Once the change is closed, a person (approver role) can finalise their edited version with `PUT /v1/changes/{id}/postmortem` (`{"markdown": "..."}`), audited as `postmortem.finalized`; from then on `GET` returns the finalised version.

### Hybrid incident memory (pgvector)

`MEMORY_MODE=hybrid` replaces the TF-IDF memory with search by meaning and by words on Postgres with pgvector, behind the same interface, so no agent changes.

- **What is remembered:** each resolved incident's resolution, plus the diagnosis, change, checks and lessons sections of its **finalised** postmortem (never a machine draft). Indexed at startup for anything missing, when an outcome is learned, and when a postmortem is finalised. An embedding outage is logged and never blocks either.
- **How it searches:** nearest vectors (HNSW, cosine) and Postgres full-text search find candidates, reciprocal rank fusion orders them, and **meaning decides whether one counts**: the score is the cosine rescaled above `MEMORY_SEMANTIC_FLOOR`, so shared words alone never make an incident "similar". Each match says why (similar in meaning, shares words, from a postmortem, same service).
- **Agents** may narrow a search by `issue_type` (one of the known types; anything else is refused).
- **Checked with** `scripts/eval_memory.py`: 9 queries describing a past incident in other words, and 4 about things no past incident covers. With Gemini `gemini-embedding-001`: lexical found the right incident for 2 of 9; hybrid at the default floor for 9 of 9, the right one first each time, and matched none of the 4 unrelated queries. The same on pgvector as in memory. The set is small and was written with the code, so this chose the floor; it is not an accuracy figure.

```bash
PYTHONPATH=src python scripts/eval_memory.py --embedder real --show
``` `/v1/metrics/summary` also reports how the agents are doing: runs, share ending in a verified fix, escalation rate, human override rate and average run time.

## Dashboard Demo

`/dashboard` is a working screen for the people in the loop, not only a demo page:

- **Live list.** The change list and an inbox ("2 awaiting approval, 1 deploying...") update by themselves; click an inbox item to jump to that change. It polls a small overview endpoint (every 1.5s while agents or a deploy are working, otherwise every 4s), pauses while the tab is hidden, and has a Pause button. A change's detail reloads only when that change actually changed.
- **Approval screen.** When a verified fix waits for a decision it shows the diagnosis, the files and the **diff the agents verified** (with a fingerprint), the Verifier's checks and before/after alert metric, the Critic's risk and concerns, and the policy gate. Approve stays disabled until the reviewer ticks "I have read the diff...". Approving never deploys anything.
- **Promotion screen.** Shows who approved and why, what is being released, and whether the release key belongs to a different person than the approver. If separation of duties would refuse it, the button is disabled and says so.
- **Evidence and timeline.** The evidence the Investigator collected (what supports the diagnosis is marked) and a plain-language timeline of everything that happened: agents, people and the platform, with each agent's tool calls folded into one line.
- **Closed buttons say why.** Run Agents, Approve, Reject, Promote and Refresh Build Status are enabled from what the server would accept, and a closed one shows the reason.
- Also: API key input, metrics, mock and simulated incidents, scheduler controls, `prepare-pr` and dev validation.

## One-Command Demo Check

Use the quick local checker in `demo_project/`:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects
$env:PYTHONPATH="C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" demo_project\run_demo.py
```

Details: `demo_project/README.md`

## Triage Evaluation Harness

Run heuristic benchmark:

```powershell
$env:PYTHONPATH = "C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" scripts\evaluate_triage.py --mode heuristic
```

Output:

- `eval/triage_eval_report.json`

LLM mode:

- set `LLM_API_KEY`
- run `scripts/evaluate_triage.py --mode llm`

## Docker Compose Demo (API + Postgres)

```powershell
cd C:\Personal_projects\Agentic_AI_Projects
docker compose up --build
```

Services:

- API: `http://127.0.0.1:8000`
- Postgres: `localhost:5432`

## Local APM Stack (No Dynatrace Account)

Use the bundled local stack in `local_apm_demo/`:

- Prometheus + Alertmanager + Grafana
- Sample app with intentional latency/error endpoints
- Alert bridge exposing `/v1/alerts` for scheduler polling

Quick start:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects\local_apm_demo
docker compose up --build
```

Then set in main project `.env`:

```env
APM_POLL_MODE=poll
APM_ALERTS_MODE=http
APM_ALERTS_BASE_URL=http://127.0.0.1:8085
```

Use `local_apm_demo/loadgen.py` to trigger alerts.

## Dynatrace Poller Setup (Real APM Connector)

Set:

```env
APM_POLL_MODE=poll
APM_ALERTS_MODE=dynatrace
APM_ALERTS_BASE_URL=https://<tenant>.live.dynatrace.com
APM_ALERTS_DYNATRACE_TOKEN=<dynatrace-api-token>
APM_ALERTS_TIMEOUT_SECONDS=10
```

Connector behavior:

- Polls `GET /api/v2/problems` for open problems
- Maps problems to internal `APMEvent`
- Uses `problemId` as dedup source alert id

## Testing

```powershell
$env:PYTHONPATH = "C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" -m pytest -q tests
```

## Investigator and Replay Scorecard

`src/app/agents/` holds the Investigator: it gathers evidence through the audited tools (metric anomalies, log patterns, deploys that preceded the onset, similar past incidents), computes a confidence per issue type from how many independent signals agree, and escalates to a human when the evidence is thin, ambiguous or points nowhere. With `INVESTIGATOR_MODE=llm` a model may request a few more tool calls and writes the root-cause wording, but it never sets the confidence.

Replay the built-in scenarios (mock data with known answers) against the old keyword classifier:

```powershell
$env:PYTHONPATH = "src"
python scripts\replay_investigations.py            # add --mode llm to use your configured model
```

This is a regression check on the logic, not a measure of accuracy on real incidents: the scenarios and rules were written together. It is not yet wired into the live incident flow.

## Verifier and Verification Scorecard

The Verifier (`src/app/agents/verifier.py`) independently checks a proposed fix. It is deliberately not model-driven and never reads the Fixer's explanation. It recomputes what changed from the git worktree (so edits made behind the sandbox or to protected files are caught), compares that with what the Fixer claimed, then runs ordered checks: scope, syntax, the project's tests, a dev deploy, whether the alert metric actually recovered (below its threshold and improved by at least `VERIFIER_MIN_IMPROVEMENT`), and whether any other metric got worse. A change that fails an early check is never deployed. Missing data is `inconclusive`, never a pass.

```powershell
$env:PYTHONPATH = "src"
python scripts\replay_verifications.py
```

This replays 13 good and bad fixes (a fix that does nothing, a partial fix, one that breaks another metric, a CI file edited behind the sandbox, a stray `.env`, ...) against a simulated dev environment. It is a regression check on the Verifier's logic, not proof about real fixes, and it is not yet wired into the live incident flow.

## Fixer and Critic

The Fixer (`src/app/agents/fixer.py`) is a model-driven tool loop that edits files in an isolated git worktree: it can list, read, grep and edit within the sandbox guardrails (allowed paths, size cap, no secrets, no CI or deploy files), but it cannot run tests, deploy, or decide that its change works. With `FIXER_MODE=off` (the default) it proposes nothing. The files it claims to have changed are read back from the worktree, never taken from the model.

The Critic (`src/app/agents/critic.py`) reviews a fix that has already passed verification. Rules check that verification really passed, that the diagnosis was confident, that the changed files are the ones the evidence implicates (scope creep is sent back), and how risky the change is (touching payment, auth or schema paths, many files, many lines). `CRITIC_MODE=llm` lets a model add concerns, but never approve or clear a failed check. `approve_for_human` is not an approval: a person still decides.

## Agent Run (Investigator -> Fixer -> Verifier -> Critic)

`src/app/agents/graph.py` is a LangGraph graph that runs the four agents for a change:

```text
investigate -> fix -> verify -> review -> ready for a person to approve
                 ^        |         |
                 +-- retry (clean worktree, up to AGENT_MAX_FIX_ATTEMPTS) --+
any stage -> escalated, with the reason written on the change
```

A run ends in exactly one of two places: `ready_for_prod` (a verified, reviewed fix waiting for a person) or `escalated`. It can never approve or promote: those still need an approver, then a different release operator, through the same endpoints and policy gate as before. The Verifier's evidence becomes the change's dev-execution evidence and the Investigator's evidence-based confidence replaces the keyword guess, so the policy gate judges what the agents actually found.

`AGENT_RUN_MODE` (default `off`, so nothing changes until you opt in):

| Mode | Behavior |
|---|---|
| `off` | No runs. |
| `manual` | `POST /v1/changes/{id}/investigate` (or the dashboard's **Run Agents** button) starts a run in the background. |
| `auto` | Every new incident, from the API or the scheduler, starts a run. |
| `shadow` | Every new incident gets an observe-only run (see Shadow Mode below). Real runs are refused. |

Try it without a real alert: `GET /v1/scenarios` lists simulated incidents and `POST /v1/incidents/scenario/{name}` opens a change from one (the dashboard has a picker). That endpoint is refused when real metrics are configured. `GET /v1/changes/{id}/agent-run` returns the reports, and the dashboard shows the diagnosis, evidence, each attempt's checks and the Critic's concerns.

Notes:
- With `FIXER_MODE=off` (default) a run investigates, writes its diagnosis on the change, then escalates; it never invents a change. Set `FIXER_MODE=llm` and an `LLM_API_KEY` to let it propose fixes.
- Each run works on its own branch, `agent-fix/<change>-<service>`, in a disposable git worktree under `.agent_worktrees/`. Worktrees are kept after a run so you can inspect them; they are git-ignored.
- By default in-flight run state is held in memory and a restart escalates any run it interrupted. Set `CHECKPOINT_BACKEND=sqlite` (or `postgres`) to save progress after every stage: on restart an interrupted run is resumed, redoing the interrupted fix attempt from clean code, at most `AGENT_RUN_MAX_RESUMES` times (default 2), with its budget carried over.


## Safety and Governance

- Assistive autonomy only: no automatic approval or production promotion
- A person who approves a change cannot promote it
- Promotion is validated and reserved (`promoting`) before any Jenkins call; a still-running deploy is never recorded as promoted
- Failed Jira/Jenkins calls are recorded as failed (no simulated tickets or builds) and block the change from production
- Writes are versioned per record (Postgres) and atomic (JSON)
- Every state change is recorded in an append-only audit log (JSONL file, or a Postgres table that rejects UPDATE/DELETE) with the real actor and the before/after state; with Postgres the audit row and an `incident.events` outbox event are written in the same transaction as the change
- State changes must follow the lifecycle table in `src/app/services/lifecycle.py`; anything else is refused
- Agent tools (`src/app/tools/`) are least-privilege: each agent gets only the methods it needs, every call is audited and counted against a per-run budget, and the repo sandbox refuses path escapes, secrets, CI/deploy files and oversized edits
- Related alerts (same service + environment, within `CORRELATION_WINDOW_MINUTES`) are folded into one open change instead of opening one per alert
- Policy gate before `ready_for_prod`
- Role-protected production promotion
- Structured audit fields in change records
