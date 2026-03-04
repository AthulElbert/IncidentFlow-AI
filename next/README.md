# Agentic Production Support Platform - Detailed Guide

## 1. What This Project Does

This project is an assistive, agentic production-support system for incident handling and safe release flow.

It takes incidents from APM-style inputs, analyzes them, prepares recovery actions, and drives controlled delivery with human approval gates.

Core capabilities:
- Monitor/ingest incident evidence (mock APM or HTTP APM adapter)
- Classify issue type and detect patterns
- Find similar historical incidents and previously successful solutions
- Generate triage plus root-cause hypotheses (heuristic or LLM)
- Create Jira ticket (mock or real)
- Trigger Jenkins dev validation (mock or real)
- Collect dev evidence and apply policy checks
- Require human decision before production readiness
- Trigger production deployment through Jenkins with audit metadata

## 2. Problem It Solves

Production teams often lose time in repetitive work:
- triaging recurring incidents
- searching old tickets/runbooks
- manually coordinating Dev -> Approval -> Prod steps
- proving fix quality before release

This platform reduces manual toil while keeping humans in control for risky decisions.

## 3. Agentic Behavior (What Is Autonomous vs Human-Controlled)

Autonomous (assistive automation):
- Incident analysis and classification
- Pattern detection and similar-incident retrieval
- Triage hypothesis generation
- Ticket and dev-job orchestration
- Policy pre-check evaluation

Human-controlled:
- Approval decision (`approve`/`reject`)
- Promotion to production

The design goal is safe autonomy: automate repetitive analysis and execution, but keep final release ownership with humans.

## 4. End-to-End Workflow

1. Incident is submitted to API (`/v1/incidents/process` or `/v1/incidents/mock`).
2. Pipeline enriches incident with:
   - issue classification
   - recurrence/pattern signal
   - similar incidents and prior solution context
   - triage output (issue type, confidence, probable cause, hypothesis steps)
3. Jira ticket is created.
4. Change record is created with metadata and warnings.
5. Dev validation is executed via Jenkins (`/v1/changes/{id}/execute-dev`).
6. APM evidence validates outcome (improvement %, smoke test pass/fail).
7. Human approver sets decision (`/v1/changes/{id}/decision`).
8. Policy gate decides whether change is `ready_for_prod` or blocked.
9. Release operator promotes via (`/v1/changes/{id}/promote`).

## 5. Technical Architecture

Main building blocks:
- `FastAPI` API layer for incident/change operations
- `Pipeline` orchestration service for end-to-end flow
- Adapter abstraction for external systems:
  - Jira adapter
  - Jenkins adapter
  - APM adapter
  - LLM adapter (OpenAI-compatible)
- Persistent storage backend:
  - `json` (local files for MVP/dev)
  - `postgres` (shared persistence for multi-instance deployment)

Key implementation characteristics:
- env-driven integration mode switching (`mock`/`real`)
- env-driven storage switching (`STORAGE_BACKEND=json|postgres`)
- graceful fallback to mock adapters on runtime integration failure
- structured logs for traceability
- role-based access control with API-key, JWT, or hybrid auth modes

## 6. Triage and Root-Cause Hypothesis Engine

The triage engine supports two modes:
- `heuristic`: deterministic local logic
- `llm`: model-generated triage/hypotheses through OpenAI-compatible API

Behavioral safeguards:
- strict response-shape validation
- confidence floor enforcement
- automatic fallback to heuristic on timeout/invalid output/errors
- warning propagation for operator visibility

This provides AI-assisted reasoning without sacrificing reliability.

## 7. Security and Governance

Authentication:
- API key mode
- JWT mode
- Hybrid mode

Roles:
- `viewer`: read/process incidents
- `approver`: can run dev execution and approve/reject
- `release_operator`: can promote to production

Governance controls:
- confidence threshold gate
- warning-count gate (optional strict mode)
- allowed Jenkins state checks
- unknown issue-type guardrails
- mandatory dev evidence status before production readiness

## 8. API Surface (High Level)

- `GET /health`
- `POST /v1/incidents/mock`
- `POST /v1/incidents/process`
- `GET /v1/changes`
- `GET /v1/changes/{change_id}`
- `POST /v1/changes/{change_id}/execute-dev`
- `POST /v1/changes/{change_id}/decision`
- `POST /v1/changes/{change_id}/promote`

## 9. Current Scope vs Future Scope

Current implemented scope:
- Mock-first architecture with real-adapter readiness
- LLM triage/hypothesis integration with fallback safety
- Dev evidence + policy-gated production promotion flow

Recommended next scope:
- real Jira/Jenkins/APM integration hardening
- PR creation automation (assistive, human-approved)
- richer knowledge base from closed incidents
- evaluation metrics dashboard (precision/recall, MTTR impact)
- infrastructure remediation workflow (optional advanced phase)

## 10. How To Run

1. Create and activate venv.
2. Install requirements.
3. Configure `.env` (start with `.env.example`).
4. Start API using uvicorn.
5. Run tests with pytest.

See root README for exact command examples and environment variables.

## 11. Success Criteria for This Project

A successful run should show:
- incident processed with clear triage metadata
- change record created with traceable evidence and warnings
- dev execution evidence collected and policy gate evaluated
- human decision recorded
- production promotion only after policy + role checks pass

This establishes a practical foundation for an enterprise-grade agentic production support platform.
