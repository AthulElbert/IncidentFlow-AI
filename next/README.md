# Agentic Production Support Platform - Detailed Guide

## 1. Project Intent

This platform reduces production support toil by automating repetitive incident response steps while preserving human control on risky decisions.

Target outcome:

- Faster triage and fix planning
- Better reuse of historical incident knowledge
- Safer release flow with explicit policy gates
- Clear auditability for approvals and promotions

## 2. What Makes It Agentic

The system behaves as an assistive agent loop:

- Observe: ingest APM-style incident signal
- Diagnose: classify issue and infer probable cause
- Ground: retrieve similar incidents and prior resolutions
- Plan: generate fix actions + PR draft context
- Act: trigger validation pipelines
- Verify: collect dev evidence and evaluate policy
- Escalate: require human decision for approval/promotion

This is not unrestricted autonomy. It is controlled automation with governance.

## 3. Capability Map

- Incident ingestion API (`mock` and custom events)
- Heuristic + LLM triage with fallback safety
- Jira and Jenkins integration via adapter abstraction
- JSON/Postgres backend switch
- PR preparation pipeline:
  - draft PR metadata generation
  - test evidence collection (`mock` or pytest)
  - issue-aware patch artifact generation
  - optional local git branch creation
  - optional sandbox-git worktree code edits with guardrails
- Dev execution and promotion workflow
- Metrics API + visual dashboard
- Offline triage evaluation harness

## 4. Trust and Safety Design

Guardrails implemented:

- LLM output validation and confidence floor
- Automatic heuristic fallback on malformed/failed LLM calls
- Policy checks before prod readiness:
  - min confidence
  - warning constraints
  - allowed Jenkins states
  - unknown issue-type guard
  - required dev evidence pass
- RBAC with `viewer`, `approver`, `release_operator`
- Structured audit data per change record

## 5. Architecture Decisions

### Adapter-first integrations

All external systems (Jira/Jenkins/APM/LLM/PR provider) use interfaces with mock and real implementations. This enables:

- local-first development
- deterministic testing
- runtime fallback resilience

### Config-driven behavior

Modes and policy are environment-driven, so behavior can be changed without code edits:

- integration mode
- storage mode
- triage mode
- auth mode
- policy thresholds

### Storage evolution path

- MVP starts with JSON files
- Production path switches to Postgres with the same service interfaces

## 6. End-to-End Runtime Flow

1. Incident arrives (`/v1/incidents/process` or `/v1/incidents/mock`).
2. Triage engine outputs issue type, confidence, probable cause, hypothesis steps.
3. Pattern detector + knowledge base add recurrence and similar incidents.
4. Jira ticket and Jenkins dev validation are initiated.
5. Change record is persisted.
6. Approver can call `prepare-pr`:
   - creates draft PR metadata
   - runs test evidence pipeline
   - writes patch artifact file
   - optionally creates local git branch reference
   - optionally applies real file changes in sandbox git worktree
7. Approver executes dev validation and records evidence.
8. Approval decision applies policy gate.
9. Release operator promotes to prod if eligible.

## 7. Interview Demo Strategy

Recommended flow:

1. Show dashboard and metrics baseline.
2. Create mock incident.
3. Show triage metadata and change record.
4. Run `prepare-pr` and open generated patch file.
5. Execute dev validation and approval.
6. Promote to prod.
7. Show metrics changes and policy behavior.

Key message:

"Agentic automation with strong governance and measurable operational outcomes."

## 8. Current Strengths

- Clean separation of domain, adapters, and transport
- Good test coverage for critical paths
- Interview-friendly mock-first and real-integration-ready design
- Visible outcomes through dashboard and metrics

## 9. Next Technical Upgrades

High-value roadmap:

1. Real PR branch/commit/push flow with safe repo sandboxing
2. CI quality gate: benchmark regression blocker for triage models/prompts
3. Reliability hardening: retries/circuit breakers/idempotency keys
4. OTel tracing and SLO-based alerting for the agent itself
5. Policy engine externalization (OPA-style rules)

## 10. Success Criteria

The platform is successful when it consistently demonstrates:

- lower manual triage effort
- reproducible, auditable mitigation proposals
- reduced unsafe production changes
- transparent quality metrics for model and operations

