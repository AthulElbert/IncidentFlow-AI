# IncidentFlow-AI: architecture v2 (design)

Status: **design. Workstream E (LLM routing and usage) is implemented; see section 7.3. The rest is not.** It extends `docs/ARCHITECTURE.md` (Phases 0 to 7, implemented). Where this document names a third-party API (MCP SDK, LangGraph checkpointers, pgvector, OPA) it is written from general knowledge and each is listed in section 12 to be verified against current documentation before coding.

## 1. Scope

Six workstreams, chosen from the candidate list:

| # | Workstream | What changes |
|---|---|---|
| A | **MCP tool backends** (candidate 1) | Prometheus, GitHub, Jira, Jenkins and Kubernetes become reachable through MCP servers, behind the typed tool interfaces agents already use |
| B | **Registry-driven tool selection** (candidate 2) | Tools are declared once; agents pick among the tools their role permits; follow-up evidence is chosen from what the scoring says is missing |
| C | **Hybrid RAG memory** (candidate 3) | pgvector plus full-text search over resolved incidents and finalised postmortems, behind the existing `IncidentMemory` interface |
| D | **Durable runs** (candidate 7) | A persistent LangGraph checkpointer, and resuming interrupted runs instead of escalating them |
| E | **LLM routing and usage** (candidate 9) | Per-task model profiles, fallback, retries, and token and cost accounting |
| F | **Security and governance** (candidate 12) | A policy engine (OPA, optional), service-scoped authorisation, OIDC, and container-isolated test execution |

**Out of scope (decided):** graph RCA with Neo4j, the Docker/Kubernetes replay engine, autonomous self-healing, AI-observability platforms, a Next.js rewrite, predictive detection. Also not planned: Temporal (a second orchestrator beside LangGraph).

### Principles that do not change

These are the design's constraints; every workstream is checked against them in section 10.

1. **L1 autonomy.** Agents propose and prove; people decide; a different person releases.
2. **Governance lives outside the agent graph,** in tested code a model cannot influence.
3. **Evidence tags and confidence are assigned by code,** never by a model or a tool's own claims.
4. **Least privilege.** An agent can use only what its role needs, every call is counted and audited.
5. **Everything is opt-in.** Defaults keep today's behaviour; the existing test suite stays green.

## 2. Architecture at a glance

```text
                         ┌──────────────────────── Policy engine (F) ───────────────────────┐
                         │  Python (today)  |  OPA sidecar (optional)   -> allow + reasons[]  │
                         └───────────┬───────────────────────┬──────────────────┬────────────┘
   identity (F)                      │ promotion gate        │ who may act on   │ which tools may
   API key | JWT | OIDC ─► Principal │                       │ which service    │ bind to which agent
                                     ▼                       ▼                  ▼
   alert ─► intake ─► CHANGE RECORD ◄── ChangeControlStore (lifecycle, audit, events, versioning)
                           ▲
                           │ only through store methods
        ┌──────────────────┴───────────────────────────────────────────────┐
        │ LangGraph run (D: durable, resumable)                              │
        │ Investigator ─► Fixer ─► Verifier ─► Critic     (retry, re-diagnose)│
        └───┬──────────────┬──────────────────────────────┬─────────────────┘
            │ LLM calls    │ tool calls                    │ retrieval
            ▼              ▼                               ▼
     LLM router (E)   Tool registry (B)                Hybrid memory (C)
     profile -> model   spec: name, schema,             pgvector + full-text
     fallback, usage    effect, evidence parser         RRF fusion, same interface
            │              │
            ▼              ├── native backends (Prometheus, Loki, Jenkins, git worktree)
     OpenAI-compatible     └── MCP backends (A): Prometheus, GitHub, Jira, Jenkins, Kubernetes
     endpoints (cloud,         read-only for agents; writes only from governed services
     local, LiteLLM proxy)
                               test execution ─► container sandbox (F), no network, no secrets
```

## 3. Workstream A: MCP tool backends

### 3.1 The decision that shapes everything

**MCP is a backend for our typed tool interfaces. It is not a way for agents to discover arbitrary tools at runtime.**

Agents today call `tools.metrics.query(service, metric, window)`. That contract is typed, role-limited, budgeted and audited, and its output becomes evidence with tags assigned by code. If agents instead received "whatever tools the MCP server advertises", all four properties would be lost, and the server (a third party's code) would be choosing what the model sees. So:

- A tool family (`metrics`, `logs`, `deploys`, `k8s`, `scm`, `tickets`) has **one Python protocol**, as now.
- Each family can have several backends: native HTTP (today) or MCP.
- "Dynamic" means the **backend is chosen by configuration** and new families can be added by a manifest, not that a model negotiates tools.

### 3.2 Components

| Component | Responsibility |
|---|---|
| `mcp/manager.py` `McpConnectionManager` | Owns connections to configured MCP servers: start, health, reconnect, per-call timeout, circuit breaker. Runs the async MCP SDK on one dedicated event-loop thread and exposes a synchronous `call(server, tool, args)` via `run_coroutine_threadsafe`, because agents run on a thread pool. |
| `mcp/manifest.py` | Loads and validates `ops/mcp_tools.json` (below). Authoritative for everything the model may see. It lives outside `CODE_CHANGE_ALLOWED_PATHS`, and startup refuses a manifest inside an agent-editable path (the rule already enforced for the LLM routing file). |
| `tools/mcp_backends.py` | `McpMetricsTool`, `McpLogsTool`, `McpDeployHistoryTool`, `McpKubernetesTool`, ... Each implements an existing protocol by calling named MCP tools and **mapping the result into our own models** (`MetricSeries`, `LogLine`, `Deploy`). Mapping is code; a malformed result becomes a `ToolError`, which already becomes evidence of the failure. |
| `tools/factory.py` | Selects a backend per family from settings (`METRICS_MODE=prometheus|mcp|mock`, and so on), as it does for modes today. |

### 3.3 The manifest

```yaml
servers:
  prometheus:
    transport: stdio                 # or streamable-http
    command: ["prom-mcp", "--url", "${PROMETHEUS_URL}"]
    env_passthrough: [PROMETHEUS_TOKEN]   # nothing else is inherited
    pin: "sha256:..."                # version or digest, reviewed before change
  github:
    transport: streamable-http
    url: https://...
    auth_env: GITHUB_MCP_TOKEN
tools:
  - id: prom.query_range
    server: prometheus
    mcp_tool: query_range
    effect: read                     # authoritative: server annotations are only hints
    family: metrics.query
    args_schema: { ... JSON Schema ... }
    max_result_bytes: 200000
    timeout_seconds: 10
  - id: k8s.pod_events
    server: kubernetes
    mcp_tool: events_list
    effect: read
    family: k8s.events
    evidence: { kind: k8s_event, parser: parse_k8s_events }   # parser is Python we wrote
```

### 3.4 Security rules (these are the review checklist for this workstream)

| Risk | Control |
|---|---|
| **Tool descriptions are an injection channel** ("tool poisoning") | Descriptions shown to a model come from **our manifest**, never from the server. |
| **A server's annotations lie** (`readOnlyHint`) | `effect` is declared by us. A tool with `effect: write` **cannot be bound to any agent**; the loader refuses. Annotations are checked only as a consistency warning. |
| **Side effects through MCP** (open a PR, create a ticket, trigger a build) | Reachable only from governed services (the pipeline, the PR service, the promotion flow), never from agent proxies. They may use MCP as their transport, but a person's decision still triggers them. |
| **Untrusted output** | Same treatment as logs: size cap, `clean_text`, parsed by our code into typed evidence, tags assigned by us, labelled untrusted in the UI. |
| **Argument injection** | Arguments validated against `args_schema` before the call; the model never supplies a raw tool name, only a registry id. |
| **Credentials** | Per-server environment variables only; stdio servers start with a minimal environment; no secrets in the manifest or the audit log. |
| **Supply chain** | Pinned commands or digests; adding or changing a server is a reviewed change to a file in the repo. |
| **Availability** | Timeout, circuit breaker, and failure becomes a `ToolError` (evidence of the failure) exactly as today; one dead server never blocks a run. |

The existing `ToolProxy` still wraps every call, so allow-lists, budgets and audit apply unchanged. Audit details gain `backend`, `server` and `mcp_tool`.

### 3.5 What MCP adds that is new

A **Kubernetes evidence family** (read-only: pod status, recent events, restarts, OOMKilled, CrashLoopBackOff). It needs new scoring signals (for example `k8s:oom` supports `resource_saturation`), so the weights in `scoring.py` are re-balanced so each issue type still sums to 1.0, and the replay scenarios gain Kubernetes variants. This is the one place A changes the Investigator's behaviour, and it is covered by section 11.

### 3.6 Not in this workstream

Exposing IncidentFlow itself as an MCP server (for other tools to read cases). Easy to add read-only later; not needed for the goal.

## 4. Workstream B: registry-driven tool selection

### 4.1 What is wrong today

- The Investigator's optional model path and the Fixer's tool loop each have a **hand-written dispatch** (`if (tool, method) == ("logs", "search")...`) and a **hand-written prompt** describing the same tools. Adding a tool means editing three places.
- The Investigator's evidence plan is fixed; the model can ask for a few extras, but nothing helps it choose *which*.

### 4.2 Design

**Tool registry.** One `ToolSpec` per callable tool: `id`, family and method, argument JSON Schema, effect, result size cap, and an **evidence producer** (Python that turns a result into `Evidence` with code-assigned tags). The registry is the single source for: the prompt text shown to a model, argument validation, dispatch, and the per-agent allow-list that replaces the hand-maintained `AGENT_PERMISSIONS` dict (that dict becomes data in the registry, validated at startup).

**Gap-driven follow-ups.** `scoring.score_issue_type` already records `missing` (the signals an issue type would show but that were not observed). New: a deterministic `next_evidence(candidates)` maps each missing signal to the tool call that could confirm or refute it, ranked by how much confidence it could move. Two uses:

1. **No model:** the Investigator runs the top N follow-ups automatically within budget. This is dynamic tool selection with no LLM and full replay determinism.
2. **With a model:** the model is shown the same ranked suggestions and may choose among them (or request another permitted tool). Its arguments are validated; its output still becomes evidence only through our producers.

**Re-diagnose on retry.** Today a failed verification retries the Fixer with the same diagnosis. If the alert metric did not recover after an attempt, the hypothesis may be wrong. New graph edge `verify -> reselect`: after one `alert_metric_recovered` failure the run moves to the next-ranked hypothesis (if its margin is acceptable) before the next attempt; otherwise it escalates. Bounded by `AGENT_MAX_FIX_ATTEMPTS`.

### 4.3 Not changing

The baseline evidence plan stays fixed (it is what makes replay results comparable). Dynamic selection only adds follow-ups on top of it, within the existing tool budget.

## 5. Workstream C: hybrid RAG memory

### 5.1 Today

`LexicalIncidentMemory` ranks records with TF-IDF plus a same-service boost and returns *why* it matched. Only `reusable` incidents (historical or resolved) are offered. This is the contract to keep.

### 5.2 Design

**Same interface, new implementation:** `HybridIncidentMemory.search(text, service, k, exclude_ids)` returns `ScoredIncident` exactly as now. Selected by `MEMORY_MODE=lexical|hybrid` (default `lexical`; `hybrid` requires Postgres with pgvector).

**Corpus.** Documents, not only incidents:

| Kind | Source | When indexed |
|---|---|---|
| `resolution` | A `resolved` incident (resolution text built from facts) | When the learner marks it resolved |
| `postmortem` | A **finalised** postmortem | When a person finalises it |
| `seed` | Historical seed incidents | At import |

Postmortem drafts are derived on demand today and never stored. For RAG a person-edited, **finalised** postmortem must be stored: `PUT /v1/changes/{id}/postmortem` (approver role) saves the markdown with author and time, audited as `postmortem.finalized`. Only finalised ones are indexed, so memory never learns from an unreviewed machine draft.

**Storage** (Postgres):

```sql
CREATE TABLE incident_documents (
  doc_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, kind TEXT NOT NULL,
  service TEXT NOT NULL, issue_type TEXT NOT NULL,
  text TEXT NOT NULL, tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding vector(N), embedding_model TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL);
CREATE INDEX ON incident_documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON incident_documents USING gin (tsv);
```

Chunk postmortems by section (diagnosis, change, lessons) so a match points at the relevant part.

**Retrieval (hybrid):** top-K by vector cosine and top-K by full-text rank, merged by **reciprocal rank fusion**, then the same same-service boost and the `reusable`/outcome filter as today. The returned `evidence` strings say which side matched (semantic, keyword, both).

**Indexing is event-driven,** reusing the pattern of `OutcomeLearner`: a consumer on `incident.events` embeds new resolution and postmortem documents. Embeddings come from an OpenAI-compatible `/embeddings` endpoint through the router (workstream E), or a local model behind the same API. A deterministic stub embedder (feature hashing) is used in tests.

**Agentic retrieval.** `memory.search` is already a tool. Add filters the model can pass (`service`, `issue_type`, `kind`) and `memory.get(incident_id)` for the full document, both registry-declared. Retrieval text is untrusted (`clean_text`, size cap) like any other evidence.

**Changing the embedding model** means re-embedding: `embedding_model` is stored per row, search filters to the active model, and a re-index job backfills.

### 5.3 How we know it is better (the gate)

Add a **retrieval evaluation set** beside the replay scenarios: queries with the incident ids a good search should return (built from the seed history and the scenarios). Report recall@k and MRR for `lexical` and `hybrid`. Hybrid becomes the documented default only if it is not worse on this set. The set is small and written by us, so this is a regression guard and a sanity check, not an accuracy claim; the doc and the interview answer must say so.

### 5.4 Privacy

Embedding requests send incident text to the embedding provider. Provide a local-endpoint option, and an `EMBEDDING_ALLOWED=false` switch that falls back to lexical.

## 6. Workstream D: durable runs

### 6.1 Today

The graph uses `MemorySaver`. Results are saved on the change, and at startup `recover_interrupted_runs` escalates any run left `running`. A run cannot resume.

### 6.2 Design

- **Checkpointer selection:** `CHECKPOINT_BACKEND=memory|postgres|sqlite` (default `memory`). Postgres follows `STORAGE_BACKEND=postgres`; SQLite gives durable runs to the JSON demo setup.
- **State is already serialisable:** `RunState` holds plain dicts (report dumps), so it checkpoints unchanged.
- **Thread id** stays `change:run-kind:number`.

### 6.3 The hard part: resuming is only safe if each node is re-entrant

| Node | Re-run behaviour | Decision |
|---|---|---|
| `investigate` | Read-only evidence; safe | Re-run if interrupted |
| `fix` | Edits a worktree; `open()` deletes and recreates the worktree, so partial edits are lost | Resume **at the start of the attempt**, never mid-fix |
| `verify` | Re-deploys to dev; idempotent enough | Re-run; but its input (the worktree diff) must exist |
| `review` | Read-only | Re-run |
| `package` | Calls `finish_agent_run`, which requires status `running` | Make it a no-op if the run already finished |

**Rule:** the unit of resumption is the attempt. On resume, completed nodes before the attempt are kept (the investigation is not repeated); an interrupted `fix`, `verify` or `review` restarts that attempt at `fix` with the same attempt number and feedback. The diff is re-derived, not trusted from a stale worktree. This avoids the failure where verification of a lost worktree reports "no changes".

### 6.4 Guarding against a crash loop

- Persist `budget_used` (seconds, tool calls, model calls) in the run state so a restart cannot reset the budget.
- Count `resumes`; after `AGENT_RUN_MAX_RESUMES` (default 2) the run escalates with that reason.
- Add transient-error retry with exponential backoff and jitter for model and tool calls (in workstream E's client and the tool proxy), separate from the fix-attempt retry.

### 6.5 Who resumes

Phase 1 is single-instance: at startup, runs with a checkpoint are resumed, runs without one are escalated (today's behaviour). Multi-instance needs a **lease**: `agent_run.lease_owner` plus a heartbeat, a stale lease can be taken over, only the lease holder resumes. It is described here and deliberately deferred.

Shadow runs stay non-resumable (they are marked interrupted, as now).

## 7. Workstream E: LLM routing and usage

### 7.1 Today

One `OpenAICompatibleLLMClient`; an optional model name per agent (`FIXER_MODEL` and so on); no retries; **token usage in the API response is discarded**, so the run budget counts calls but not tokens.

### 7.2 Design

**Profiles, not guesswork.** Each call site declares a profile: `triage`, `investigator.reason`, `fixer.step`, `critic.review`, `embedding`. Configuration maps a profile to an ordered list of endpoints (primary, then fallbacks):

```yaml
llm:
  endpoints:
    strong:  { base_url: ..., model: ..., price_per_mtok: [in, out] }
    cheap:   { base_url: ..., model: ..., price_per_mtok: [in, out] }
    local:   { base_url: http://localhost:11434/v1, model: ..., price_per_mtok: [0, 0] }
  profiles:
    triage:             [cheap, local]
    investigator.reason: [strong, cheap]
    fixer.step:         [strong]
    critic.review:      [strong, cheap]
    embedding:          [local]
  escalate_on_retry: { fixer.step: { attempt_gte: 2, use: strong } }
```

`RoutedLLMClient` implements the existing `LLMClient` protocol; call sites change by adding a `profile` argument.

**What it does:**

1. **Fallback with a circuit breaker.** A timeout, 429 or 5xx moves to the next endpoint; repeated failures open the breaker for a cooldown. This is the highest-value part of "routing": surviving a provider outage.
2. **Difficulty from code, not from the model.** The only dynamic rule is signals we compute (for example a Fixer retry uses the stronger model). Routing by an LLM's guess of task complexity is not attempted: there is no evaluation data to justify it.
3. **Usage capture.** The client records `prompt_tokens`, `completion_tokens`, latency and endpoint for every call into a `UsageRecorder` set by the graph node (run, agent), via a context variable so concurrent runs do not mix.
4. **Token and cost budgets.** `RunBudget` gains `max_tokens` and `max_cost`; the existing `llm_gate` path enforces it for the Fixer, and the same gate is added to the Investigator and Critic. This closes the gap noted in `agents/budget.py`.
5. **Audit without content.** `llm.call` audit entries carry profile, endpoint, token counts, latency and cost, never the prompt or reply.
6. **Data tiers.** A profile can be pinned to local endpoints only, so incident text never leaves the network for that task.

**LiteLLM:** not imported. Its proxy works today as an endpoint (`base_url`), so it is compatible. Building the router in-tree is justified only because budgets, audit and data tiers must sit in our code; if the router grows beyond fallback and accounting, adopting LiteLLM behind the same interface is the right move.

### 7.3 As implemented (step 8.1: `adapters/llm_router.py`, `adapters/llm_usage.py`)

- **Profiles and fallback.** `LLMRouter` maps a profile to an ordered list of endpoints. A transient error (timeout, 408/425/429, 5xx, connection failure) is retried on the same endpoint with exponential backoff and jitter (`LLM_MAX_RETRIES`, default 2); any other error moves to the next endpoint; a circuit breaker per endpoint (3 consecutive failures, 30s cooldown, then one trial call) skips an endpoint that keeps failing. With one endpoint the original error is re-raised unchanged; with several, the error names each endpoint's failure.
- **No call-site changes.** `router.client(profile)` returns a `ProfiledLLM` that satisfies the existing `LLMClient` protocol, so agents and the test fakes are untouched. A task with no profile of its own shares the default client.
- **Configuration is JSON, not YAML** (`LLM_ROUTING_FILE`, example in `ops/llm_routing.example.json`), to avoid a new dependency. Keys are named by environment variable (`api_key_env`), never written in the file. Without a routing file the plain `LLM_*` settings behave as before, now with retries. A routing file and the per-agent `*_MODEL` settings together are refused, as is a routing file inside an agent-editable path.
- **Escalation by facts.** `escalate_on_retry` sends a profile to a named endpoint from a given fix attempt onward; the attempt number comes from the run, not from a model.
- **Local-only tasks.** `local_only_profiles` is checked at load: every endpoint such a profile can reach, including its escalation target, must be marked `local`.
- **Usage.** The HTTP client now returns the provider's token counts. The router reports every physical call (retries and fallbacks included) to a sink held in a context variable, so concurrent runs cannot mix. In an agent run the sink adds tokens and cost to the run budget and writes an `llm.call` audit entry with profile, endpoint, model, tokens, cost and latency, never content. The timeline folds these into one line per agent.
- **Budgets.** `AGENT_RUN_MAX_TOKENS` (default 600,000) and `AGENT_RUN_MAX_COST` (default 0, meaning no limit) join the call limit. The Investigator and Critic now take the same model gate as the Fixer: out of budget, the Investigator answers from the evidence and the Critic reviews by its rules.
- **A flaw this exposed in the Phase 7 budget, now fixed.** The stage-boundary check counted model limits, so a fix that used the last allowed model call was escalated before verification, which needs no model. Model limits are now enforced only where a model call is attempted; time and tool calls still stop a stage from starting.
- **`GET /v1/llm/status`** shows each profile's chain and each endpoint's model and circuit state, with no keys.
- **Verified** by 60-odd tests (including the real HTTP client against a local server) and one end-to-end run through the app with a dead first-choice endpoint: retries, fallback, the breaker opening, and tokens and cost on the run and in the audit log. **Not verified:** any real provider. Token counts depend on the provider reporting `usage`; when it does not, tokens count as zero and the record says so, so a token limit cannot bind for that provider.
- **Not done:** the `embedding` profile is reserved for workstream C; `Retry-After` headers are not honoured; prices are static numbers in the routing file.

## 8. Workstream F: security and governance

Four independent parts.

### 8.1 Policy engine

Introduce a `PolicyEngine` interface with two implementations: `PythonPolicy` (today's `_evaluate_policy`, the reference) and `OpaPolicy` (REST call to an OPA sidecar with Rego rules in `policy/`).

| Decision | Input | Output |
|---|---|---|
| Promotion gate | Change summary (confidence, warnings, Jenkins state, issue type, dev evidence) | `allow` plus `reasons[]` (the UI already shows reasons) |
| Who may act | Principal (roles, teams), action, service, environment | allow plus reason |
| Tool binding | Agent, tool spec (`effect`, family) | allow plus reason (checked at startup) |

Rules:

- **Fail closed:** an OPA error or timeout denies, and the denial says why.
- **Decision logging:** every promotion and authorisation decision is audited with the policy bundle hash, so a past decision can be explained.
- **Parity test:** one table of cases runs against both engines and must agree. It runs the Rego side only when the `opa` binary is available, and is skipped otherwise.
- **Not per tool call:** agent permissions are evaluated when a run builds its toolkit, not on every call (a network hop on a hot path buys nothing).

### 8.2 Service-scoped authorisation

Today a role is global. Add ownership: `service -> owning team` in config, `Principal.teams` from the token, and the rule "an approver or release operator must belong to the service's team (or be a platform admin)". Separation of duties stays. The review endpoint computes action availability for the requesting principal; the dashboard, which uses three keys, evaluates each in turn through `/v1/whoami`.

### 8.3 Identity (OIDC)

The JWT path already checks issuer, audience and algorithm. Add `AUTH_MODE=oidc`: fetch and cache the issuer's JWKS (with rotation), verify RS256 or ES256, map `roles`/`groups` claims to roles and teams, and keep API keys for service accounts. Tested with locally generated keys and a JWKS fixture; a real identity provider is not covered by tests.

### 8.4 Isolated test execution

**A gap in the current design, worth stating plainly:** `FileSandbox.run_tests` runs the repository's test command on the **host** (with a scrubbed environment). Tests execute code, and the Fixer edits code, so model-written code can run on the host.

Fix: `ContainerSandbox` wraps `FileSandbox`. Edits still happen through our Python code in the worktree; `run_tests()` runs in a container with no network, a read-only root filesystem and a tmpfs for scratch, all capabilities dropped, no inherited secrets, and memory, CPU, process and time limits.

Configuration: `SANDBOX_EXEC=host|container`. A governance rule in `validate_settings`: `FIXER_MODE=llm` with `SANDBOX_EXEC=host` is refused unless `ALLOW_HOST_EXECUTION=true`, so running model-written code on the host becomes an explicit, logged choice.

`dev_deploy` inherently runs branch code in the dev environment; that boundary is the dev environment's, not ours.

## 9. Phasing and dependencies

```text
Gate 0  Run a real model through the existing system (prerequisite, not a build task)
   │
   ├─► E  LLM router + usage         (first: others depend on usage and retries)
   │      │
   │      ├─► D  Durable runs        (needs transient retry and persisted budgets)
   │      └─► C  Hybrid memory       (needs the embedding profile)
   │
   ├─► B  Tool registry              (first: MCP builds on it)
   │      └─► A  MCP backends
   │
   └─► F  Governance                 (independent; 8.4 container sandbox can go early)
```

| Step | Content | Size |
|---|---|---|
| 8.0 | Real model run, prompt tuning, record what actually broke | S (time depends on prompts) |
| 8.1 | E: router, retries, usage, token/cost budget | M, **done** |
| 8.2 | F 8.4: container sandbox and the host-execution rule | M |
| 8.3 | D: checkpointer, attempt-level resume, crash-loop guard | M |
| 8.4 | B: registry, gap-driven follow-ups, re-diagnose edge | M |
| 8.5 | A: MCP manager, manifest, Prometheus and GitHub backends, then Kubernetes evidence | L |
| 8.6 | C: schema, indexer, hybrid search, finalised postmortems, retrieval eval | L |
| 8.7 | F 8.1 to 8.3: policy engine, scoped authorisation, OIDC | L |

Each step ships separately, opt-in, with its own tests and a note in the "verified / not verified" list.

## 10. Checking the principles

| Principle | A MCP | B registry | C RAG | D durable | E router | F governance |
|---|---|---|---|---|---|---|
| L1 autonomy | No write tools for agents | Same allow-list rule | n/a | Resume never approves | n/a | Strengthens it |
| Governance outside the graph | Manifest and policy, not the model | Registry is code | n/a | Store still the only writer | Budget gate in code | Is this workstream |
| Tags and confidence by code | Parsers are ours | Producers are ours | Retrieval is evidence, not a verdict | n/a | n/a | n/a |
| Least privilege and audit | Same proxy | Registry replaces the dict | Same tool | n/a | `llm.call` audit | Adds scoping |
| Opt-in, existing tests green | New modes | Behaviour-preserving first | `lexical` default | `memory` default | Single endpoint default | `PythonPolicy` default |

Two places where a principle is under real pressure and the controls matter most: **A** (a third party's server in the evidence path) and **D** (resuming code that touches a worktree).

## 11. Testing strategy

- **A:** an in-memory fake MCP server (the SDK supports connecting a client and server in one process) for manager, mapping and failure tests; manifest validation tests (write tool refused, bad schema refused, description comes from the manifest); a contract test per backend that the mapped model equals the native backend's on the same fixture; replay scenarios extended for Kubernetes evidence, with the scoring weights re-checked.
- **B:** golden tests that the registry reproduces today's prompts and dispatch; a deterministic test that a missing signal yields the expected follow-up; re-diagnose edge tests (metric fails, second hypothesis is tried, margin too thin escalates).
- **C:** stub-embedder tests for fusion and filtering; the `reusable` and untrusted-text rules; the retrieval evaluation set; a real-pgvector test gated by `TEST_DATABASE_URL`, like the existing Postgres tests.
- **D:** kill-between-nodes tests (raise after each node, restart, resume, assert the same final result and no duplicate side effects); crash-loop test (resumes cap and budget carry over); `package` idempotence.
- **E:** fake endpoints for fallback, circuit breaker, backoff, usage capture, cost arithmetic, per-run isolation of the recorder under concurrency.
- **F:** policy parity table; fail-closed tests; scoped-authorisation matrix; OIDC with generated keys (wrong issuer, audience, expired, unknown key, rotation); container sandbox tests that run only when Docker is available (no network, read-only, limits), skipped otherwise.

Existing suite: all 611 tests must pass at every step with default settings.

## 12. Assumptions to verify before coding (not checked yet)

I have not read current documentation for these, and nothing here depends on guessing silently:

1. **MCP Python SDK:** client session and transports (stdio, streamable HTTP), the in-memory client/server pairing for tests, tool annotations. Also which Prometheus, Kubernetes, Jenkins and Jira MCP servers exist, are maintained and are safe to pin (GitHub publishes an official one, to be confirmed).
2. **LangGraph checkpointers:** the Postgres and SQLite saver packages, `setup()`, how to inspect the last completed node and resume, and whether state updates can restart at a chosen node.
3. **pgvector:** extension availability on the target Postgres, HNSW syntax and the Python adapter.
4. **OPA:** REST decision API shape, bundle loading, `opa eval` for tests.
5. **PyJWT** with RS256/ES256 and JWKS handling (needs the `cryptography` extra).
6. **Docker flags** for the container sandbox on Windows and Linux (the local Docker daemon was not running earlier in this project, so workstreams F 8.4 and C's real-database test cannot be verified here until it is).

## 13. Risks

| Risk | Mitigation |
|---|---|
| MCP servers are immature or unmaintained | Backend is swappable; native backends stay; pin and review |
| Resume re-runs a side effect | Attempt-level resume, idempotent `package`, kill-between-nodes tests |
| Hybrid search is not actually better | The evaluation gate; `lexical` stays the default until it passes |
| New dependencies and services (pgvector, OPA, Docker) | Every one optional and behind an interface; documented fallbacks |
| Scope: six workstreams is a lot | Section 9's order; each step ships alone; stop after any step |
| More claims to defend | Each step adds an honest verified / not-verified entry |

## 14. Decisions needed

1. **Order.** Recommended: 8.0, 8.1, 8.2, 8.3 first. They fix stated weaknesses (no real model, no token budget, host-executed model code, non-durable runs) before adding capability.
2. **Container sandbox now or later.** Recommended now (8.2): it is a security fix, but it needs Docker running to verify.
3. **OPA or Python-only policy.** Recommended: build the `PolicyEngine` interface and scoped authorisation in Python first; add OPA only if you want it for the portfolio story.
4. **Which MCP servers.** Recommended: Prometheus and GitHub first (read-only), Kubernetes second. Jira and Jenkins stay on their native clients until an MCP server proves better.
5. **Embedding provider.** Recommended: a local OpenAI-compatible endpoint if you have one, otherwise the same provider as the chat model, with `EMBEDDING_ALLOWED=false` as the fallback.
