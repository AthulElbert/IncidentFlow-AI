# Production Support Agent MVP (Jira + Jenkins Ready)

This project is an assistive production-support agent that:

1. Accepts APM-like alerts.
2. Classifies the issue type.
3. Detects recurring patterns.
4. Finds similar past incidents and prior resolutions.
5. Creates Jira tickets (mock or real).
6. Triggers Jenkins dev validation (mock or real).
7. Collects APM evidence for dev-fix validation (mock or HTTP integration).
8. Requires human approval and policy checks before prod readiness.
9. Triggers Jenkins prod deploy with audited promotion metadata.

## Tech Stack

- Python 3.10+
- FastAPI
- Pydantic
- python-dotenv
- PyJWT
- Env-driven adapters for Jira, Jenkins, and APM

## Project Structure

```text
src/app/main.py                         # API entrypoint + RBAC enforcement
src/app/config.py                       # Env settings + .env loading
src/app/security.py                     # API-key/JWT auth + role checks
src/app/logging_config.py               # JSON structured logging
src/app/models/schemas.py               # Request/response models
src/app/services/classifier.py          # Issue classification heuristics
src/app/services/pattern_detector.py
src/app/services/knowledge_base.py      # Similar incident lookup + local store
src/app/services/fix_planner.py         # Runbook/config action suggestions
src/app/services/integration_factory.py # Selects mock vs real adapters
src/app/services/pipeline.py            # End-to-end orchestration + fallbacks
src/app/services/dev_fix_executor.py    # Jenkins + APM evidence based dev validation
src/app/services/change_control.py      # Approval gate + policy checks + persisted change records
src/app/adapters/jira_client.py         # Jira adapters
src/app/adapters/jenkins_client.py      # Jenkins adapters (dev + prod)
src/app/adapters/apm_client.py          # APM evidence adapters (mock + http)
data/incident_history.json              # Seeded incident history
data/change_records.json                # Change approval records
```

## Setup

```bash
cd C:\Personal_projects\Agentic_AI_Projects
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

1. Copy `.env.example` to `.env`.
2. Fill values for your environment.
3. `main.py` auto-loads `.env` at startup.

Policy settings:
- `MIN_CONFIDENCE_FOR_PROD`
- `REQUIRE_ZERO_WARNINGS_FOR_PROD`
- `ALLOWED_JENKINS_STATES_FOR_PROD`

Dev evidence settings:
- `APM_MODE`: `mock` | `http`
- `APM_BASE_URL`, `APM_TIMEOUT_SECONDS`, `APM_VERIFY_SSL`
- `DEV_MIN_APM_IMPROVEMENT_PCT`
- `REQUIRE_DEV_SMOKE_TESTS`

Auth settings:
- `AUTH_ENABLED`
- `AUTH_MODE`: `api_key` | `jwt` | `hybrid`
- API keys: `VIEWER_API_KEY`, `APPROVER_API_KEY`, `RELEASE_OPERATOR_API_KEY`
- JWT: `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_ROLE_CLAIM`, `JWT_ACTOR_CLAIM`

## Run API

PowerShell:

```powershell
$env:PYTHONPATH = "C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

API docs:
- http://127.0.0.1:8000/docs

## Run Tests

```powershell
$env:PYTHONPATH = "C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" -m pytest -q tests
```

## APM HTTP Evidence Contract

When `APM_MODE=http`, app calls:

- `GET {APM_BASE_URL}/v1/evidence?service=<>&change_id=<>&issue_type=<>`

Expected JSON response:

```json
{
  "apm_improvement_pct": 8.5,
  "smoke_tests_passed": true,
  "notes": "latency dropped after config update"
}
```

## RBAC Model

Role permissions:
- `viewer`: read endpoints + incident ingestion
- `approver`: viewer permissions + execute dev + approval decision
- `release_operator`: approver permissions + promotion

## Authentication Modes

1. `api_key`
- Send header: `X-API-Key: <key>`

2. `jwt`
- Send header: `Authorization: Bearer <token>`
- Token must include role and actor claims (configurable by `JWT_ROLE_CLAIM` and `JWT_ACTOR_CLAIM`).

3. `hybrid`
- Accepts JWT bearer token or API key.
- If bearer token is present, it is validated first.

## Endpoints

- `GET /health` (viewer+)
- `POST /v1/incidents/mock` (viewer+)
- `POST /v1/incidents/process` (viewer+)
- `GET /v1/changes` (viewer+)
- `GET /v1/changes/{change_id}` (viewer+)
- `POST /v1/changes/{change_id}/execute-dev` (approver+)
- `POST /v1/changes/{change_id}/decision` (approver+)
- `POST /v1/changes/{change_id}/promote` (release_operator)

## Typical Flow

1. Process incident using `/v1/incidents/process` or `/v1/incidents/mock`.
2. Read `metadata.change_id` from response.
3. Review change using `GET /v1/changes/{change_id}`.
4. Execute dev fix with `POST /v1/changes/{change_id}/execute-dev`.
5. Approve/reject using `POST /v1/changes/{change_id}/decision`.
6. If `deployment_state=ready_for_prod`, promote using `POST /v1/changes/{change_id}/promote`.
7. Promotion triggers Jenkins prod job and stores queue/build URL + result.

## Policy Gate Behavior

When a change is approved, policy checks run before marking it ready for production:
- confidence must meet minimum threshold
- warnings must be zero (if configured)
- Jenkins status must be in allowed states
- unknown issue type is blocked for manual deep-dive
- dev execution evidence must be in `passed` state

Decision outcomes:
- `approved + ready_for_prod` (policy passed)
- `approved + blocked_by_policy` (approved by human but blocked for final rollout)
- `rejected + closed_rejected`

Promotion outcomes:
- `promoted_to_prod` when Jenkins prod trigger is queued/success
- `prod_promotion_failed` when Jenkins prod trigger returns non-success state

## Structured Logging

- Logs are emitted as JSON lines.
- Set `LOG_LEVEL` in `.env` (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- Pipeline logs include incident ingestion, fallback usage, and processing completion.

## Enabling Real Jira + Jenkins

1. Set `JIRA_MODE=real` and `JENKINS_MODE=real`.
2. Fill required Jira/Jenkins credentials in `.env`.
3. Start the API and call `/v1/incidents/mock`.

Notes:
- If real integration fails at runtime, pipeline auto-falls back to mock and includes warnings in `metadata.warnings`.
- `/health` shows active startup modes and auth source (`api_key` or `jwt`).

## Assistive Behavior

- Agent suggests and prepares actions.
- Human approval is required.
- Policy checks enforce safe promotion criteria before `ready_for_prod`.
- Promotion is role-protected and audited.
