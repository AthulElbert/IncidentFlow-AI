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
- Test evidence:
  - `TEST_EVIDENCE_MODE`: `mock` | `pytest`
  - `TEST_EVIDENCE_COMMAND`
  - `TEST_EVIDENCE_TIMEOUT_SECONDS`
- Triage:
  - `TRIAGE_MODE`: `heuristic` | `llm`
  - `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- Integrations:
  - `JIRA_MODE`, `JENKINS_MODE`, `APM_MODE`
- Policy:
  - `MIN_CONFIDENCE_FOR_PROD`
  - `REQUIRE_ZERO_WARNINGS_FOR_PROD`
  - `ALLOWED_JENKINS_STATES_FOR_PROD`
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
- `GET /v1/changes`
- `GET /v1/changes/{change_id}`
- `GET /v1/metrics/summary`
- `POST /v1/changes/{change_id}/prepare-pr`
- `POST /v1/changes/{change_id}/execute-dev`
- `POST /v1/changes/{change_id}/decision`
- `POST /v1/changes/{change_id}/promote`

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

## Dashboard Demo

`/dashboard` supports:

- API key input
- Metrics refresh
- Mock incident generation
- `prepare-pr` trigger by `change_id`

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

## Testing

```powershell
$env:PYTHONPATH = "C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" -m pytest -q tests
```

## Safety and Governance

- Assistive autonomy, human-controlled release decisions
- Policy gate before `ready_for_prod`
- Role-protected production promotion
- Structured audit fields in change records
