# How to talk about IncidentFlow-AI in an interview

This is a speaking guide, not documentation. The code and `docs/ARCHITECTURE.md` hold the facts; this tells you which facts to lead with, how to say them, and where to be careful. Read the whole thing once, then practise the pitch and the demo out loud.

The one idea to keep returning to:

> **The agents propose and prove. People decide. The platform makes sure nothing skips that.**

Every design choice below is a consequence of that sentence.

---

## 1. The 30-second answer

> "IncidentFlow is a production-support platform. When a monitoring alert fires, four AI agents investigate it, try a fix in an isolated copy of the code, verify the fix independently, and have a reviewer agent challenge it. A verified fix then waits for a person to approve it, and a *different* person has to release it. The interesting part isn't the AI. It's the governance around it: every state change is validated and audited, the agents have least-privilege tools, and the system can run in shadow mode so you can see what the agents would have done before you trust them with anything."

Stop there. Let them pull on a thread.

## 2. The 2-minute answer (when they say "tell me more")

Cover four things, in this order:

1. **The problem.** Alerts arrive faster than people can triage them, and a wrong automated fix in production is worse than a slow human one. So the goal is to automate the *investigation and the proof*, not the decision.
2. **The flow.** Alert → a change record is opened → the Investigator gathers evidence from metrics, logs, deploy history and past incidents → the Fixer edits code in a throwaway git worktree → the Verifier runs the tests, deploys to dev and checks the alert metric actually recovered → the Critic reviews → a verified fix waits for an approver → a different release operator promotes it.
3. **The two decisions that shaped everything.** Autonomy stays at "L1": agents never approve or release anything. And governance lives in plain, tested Python *outside* the agent graph, so the part that matters for safety isn't something a model can talk its way around.
4. **How I know it works.** 611 automated tests, a replay harness, and I ran the whole flow in a real browser against a real git repo and real pytest, which found bugs the tests had missed.

## 3. Draw this on the whiteboard

```text
 alert ──► intake/triage ──► CHANGE RECORD (versioned, lifecycle-checked, audited)
                                   │
              ┌────────────────────┴─────────────────────┐
              │  LangGraph run (the "brain")             │   governance is NOT in here:
              │  Investigator → Fixer → Verifier → Critic│   it is ChangeControlStore methods
              │            ↑___retry (max 3)___↓         │   the graph calls, which enforce the
              └────────────────────┬─────────────────────┘   lifecycle and write the audit log
                                   ▼
                 ready_for_prod  (still "pending approval")
                                   ▼
                 Approver ──► Release operator (must be a different person) ──► production
                                   ▼
                 outcome → event bus → incident memory + postmortem draft
```

Say out loud: *"The graph decides what happens next. It never decides what's allowed."*

---

## 4. The decisions, and the reason for each

Interviewers are listening for **trade-offs**. For each decision give: what, why, and what it costs.

| Decision | Why | The honest cost |
|---|---|---|
| **Four agents, not one** | Separation of duties applies to software too: the thing that writes a fix shouldn't be the thing that judges it. The Verifier ignores the Fixer's rationale and is model-free. | More moving parts and more latency than one agent with tools. |
| **Autonomy stays at L1** | A wrong auto-release costs far more than a slow human. Also easier to get approved by a security team. | No time saved on the final decision. |
| **LangGraph for the loop, own Python for governance** | LangGraph gives retries, routing and state for free. But the *rules* (who may approve, which transitions are legal) must be testable and not model-driven. | Two layers to understand. |
| **Confidence is computed by code, not asked of the model** | Models are poorly calibrated about themselves. The score comes from how many independent signals agree (metric anomaly, deploy timing, log pattern, past incident). | The scoring rules are hand-written, so they only know the signal types I thought of. |
| **The Verifier is code-first and model-free** | If a model verifies a model's work, you've verified nothing. It runs tests, deploys to dev, and checks the *alert metric itself* recovered. | It can only check what has a measurable signal. |
| **Least-privilege tool proxies** | Only the Fixer can edit; only the Verifier can run tests or deploy to dev; nobody can roll back or promote. Every call is counted and audited, denied attempts included. | It's an in-process boundary, not a sandbox against hostile code. I say so. |
| **Reserve-then-call promotion** | The change is moved to `promoting` *before* the deploy is triggered, so a double-click or retry can't deploy twice. Only a confirmed Jenkins `SUCCESS` becomes `promoted_to_prod`. | A promotion can sit in "deploying" until someone refreshes the status. |
| **Separation of duties** | The person who approves can't be the person who promotes. Enforced on the server, mirrored (not trusted) in the UI. | Needs at least two people in the loop. |
| **Pull-based event bus, Kafka-ready** | `poll`/`commit` maps directly onto Kafka consumer groups. Delivery is at-least-once, so every consumer is idempotent. | I built in-process, Postgres-outbox and a Kafka adapter, but the Kafka one has only met a fake client. |
| **Shadow mode before trust** | Run the agents on real incidents with no effect, then compare with what people did. That's how you earn autonomy with evidence. | Costs a model run per incident. |
| **Polling, not push, for the dashboard** | Browsers can't send an API-key header on a push connection, and polling works behind any proxy. | A few seconds of delay. |

### If they ask "why LangGraph and not Camunda or Bedrock Agents?"

> "Camunda is a workflow engine for deterministic business processes; this loop is a cycle with model-driven branching and retries, which LangGraph models directly. Bedrock Agents would couple me to one cloud and one model, and I wanted the control logic to be mine. LangGraph gives me the graph; I kept the governance out of it on purpose, so swapping the orchestrator wouldn't touch the safety rules."

---

## 5. Stories to tell (these are your best material)

Interviewers remember a specific bug far better than a feature list. Use the **situation → what I found → what I changed → what I learned** shape. Pick two or three.

**1. "The tests passed, the real run didn't."** I ran the full flow in a real browser against a real git repo. It found three defects the unit tests couldn't: re-running a change failed on a stale git worktree; the agent's branch name collided with another flow's, which would have reset someone's commits; and the Investigator found *the incident it was investigating* in incident memory and counted it as corroborating evidence, inflating its own confidence. *Lesson: mocks agree with your assumptions; reality doesn't.*

**2. "Memory that poisoned itself."** Every new incident was saved with a copy of the most similar past incident's resolution, so a resolution could be inherited down a chain of incidents nobody had resolved, and later offered as evidence. The fix was to give each incident an outcome (`open`, `resolved`, `rejected`) and only let resolved ones teach, with the resolution written from facts (what changed, how it was verified, who approved) rather than by a model. *Lesson: a learning loop needs a notion of ground truth.*

**3. "A deploy marked successful before it deployed."** The original promote flow could mark a change as promoted while the Jenkins build was still queued. I changed it to reserve the change first, trigger the deploy, and only accept a confirmed `SUCCESS`. *Lesson: never let the success path be the default.*

**4. "A one-character-class bug that mattered."** `lstrip("./")` strips *characters*, not a prefix, so `.env` became `env`, so path checks (such as the sandbox's protected files) were comparing the wrong name. I replaced it with a real path normaliser and added tests. *Lesson: security checks fail in boring ways.*

**5. "The test caught my own new bug."** When I added a shared tool-call budget, the test showed a call refused by an agent's own cap still spent one of the run's shared calls. Fixed by checking the agent's cap first and refunding on a refusal. *Lesson: write the test for the edge, not just the happy path.*

**6. "Prompt injection is a design input."** One of the built-in scenarios puts an instruction inside a log line. Logs and deploy notes are treated as untrusted text, the agent can't act on instructions found in them, and the evidence tags that drive confidence are assigned by code from the data, never by a model.

---

## 6. How to talk about testing

- **611 automated tests**, hermetic by design (every test runs against throwaway data; one early bug was tests polluting tracked files).
- **A replay harness** runs seven built-in incident scenarios and reports accuracy against the old keyword classifier, plus a "harmful" count (proceeding with a wrong answer).
- **The honest caveat, say it before they ask:** *"The scenarios and the scoring rules were written together, so that's a regression check, not an accuracy estimate for real incidents."* This one sentence buys a lot of credibility.
- **A test that the UI cannot disagree with the server.** The dashboard's enabled/disabled buttons come from the same functions the server uses to accept or refuse an action, and a test walks a change through every stage checking both agree. I also broke the logic on purpose once to confirm the test would fail.
- **Real end-to-end runs** in a browser, with a local stand-in for the model API, real git worktrees and real pytest.

## 7. What is *not* done (say it first, plainly)

Being upfront here is a strength. Don't wait to be caught.

- **No real LLM has been run through it.** Everything model-driven was tested with a scripted model and a local stand-in server. I expect prompt tuning to be the biggest remaining unknown.
- **Kafka has only been tested against an in-memory stand-in**, not a real broker.
- **In-flight agent state is in memory.** Results are saved on the change, and a restart escalates interrupted runs, but a run can't resume mid-flight.
- **Nothing polls Jenkins in the background,** so a promotion needs a manual status refresh.
- **It's demo-level.** The architecture is built for a real environment (Prometheus, Loki, Jenkins, Postgres, Kafka are all interfaces), but I implemented mock and local backends.

A good line: *"I'd rather tell you what I haven't verified than have you find it."*

---

## 8. Questions you'll probably get, with model answers

**Why multiple agents instead of one?**
Because the failure mode of a single agent is marking its own homework. Splitting investigation, fix, verification and review gives independent checks, and the Verifier doesn't use a model at all.

**How do you stop the AI doing something dangerous?**
Layers. Agents hold only the tools their role needs; only the Fixer can edit and only inside a disposable worktree with protected files; no agent can approve or promote; the lifecycle table rejects illegal state changes; a human approves and a different human releases; and every call is audited.

**How do you measure whether the agents are any good?**
Not with one number. I track fix-ready rate, escalation rate, human override rate (of verified fixes a person decided on, how many they rejected) and run time. Shadow mode compares what the agents would have done to what people did. I'd want weeks of that before discussing more autonomy.

**What happens if the model hallucinates?**
Its output is never trusted directly. Confidence is computed from observed signals. The Fixer's claimed files are taken from the actual diff. The Verifier re-checks everything with code. A model that proposes an invalid action or unknown evidence is overruled and flagged.

**How do you handle concurrency and double-submits?**
Optimistic versioning on every change record (a version column; a conflict returns 409 and retries), a reservation step for agent runs and promotions so a second request is refused, atomic file writes for the JSON backend, and idempotent consumers for the at-least-once event bus.

**What if two alerts arrive for the same problem?**
They're correlated onto one change within a time window, so one incident means one investigation.

**How would you take this to production?**
Swap the mock tool backends for the real ones (they're behind interfaces), use the Postgres store and outbox, put Kafka behind the relay, replace API keys with SSO, add a background job to track Jenkins, run shadow mode for a few weeks, and only then consider enabling agent runs for one low-risk service.

**What would you do differently?**
Run against a real model much earlier. I built a lot of correct plumbing before ever seeing a real model's behaviour, and that's where the surprises will be.

**What's the hardest part?**
Deciding what the AI is *not* allowed to do and making that enforceable rather than a convention. The model code was the smaller part.

**Why not just fully automate it?**
Because the cost of being wrong in production is asymmetric. Shadow mode exists precisely to make the case for more autonomy with data, instead of asserting it.

**How do you control cost?**
Per-run budgets for time, total tool calls and model calls; per-agent tool caps; bounded retries (max 3); and escalation instead of looping when a limit is hit. Shadow mode does cost a run per incident, which I'd rate-limit.

---

## 9. A three-minute demo script

Run with `AGENT_RUN_MODE=manual`. Narrate *what the platform is protecting*, not what you're clicking.

1. **Open the dashboard.** Point at the live inbox: "it updates on its own."
2. **Create a simulated incident** (`bad_config_latency`). "This is a config deploy that tripled latency."
3. **Run Agents.** While it works: "Investigator is gathering evidence from metrics, logs and deploy history. The Fixer is editing in a throwaway git worktree."
4. **Open the approval screen.** Show the diagnosis and evidence, the diff, the Verifier's checks and the before/after alert metric. "The reviewer sees exactly what was verified, with a fingerprint of the diff."
5. **Show the Approve button disabled** until the checkbox is ticked. "Approving deploys nothing."
6. **Approve, then show the promotion screen.** "Whoever holds the release key must be a different person. If they're not, the button is disabled and says why."
7. **Promote**, then open the timeline and the postmortem draft. "Every step is in the audit trail, and the postmortem is built from facts and leaves the judgement to people."
8. **Close with shadow mode:** "Before enabling any of this for real, I'd run it in shadow and compare."

If the live demo breaks, fall back to the timeline and review screens from a finished change. They tell the same story.

---

## 10. Delivery tips

- **Lead with the sentence in the box at the top.** Come back to it whenever a question wanders.
- **Say "propose and prove" rather than "autonomous".** The word "autonomous" invites worries you've already designed away.
- **Name the trade-off every time.** "I chose X because Y; the cost is Z." That's what senior sounds like.
- **Use numbers you actually know:** 4 agents, 7 phases, 611 tests, 7 built-in scenarios, 3 attempts max, one human to approve and a different human to release.
- **Don't claim what you haven't verified.** The list in section 7 is your shield. Using it unprompted makes everything else more believable.
- **If you don't know:** "I haven't tested that. Here's how I would." Then say how.
- **Practise out loud.** The 30-second answer and the whiteboard sketch are the two things worth rehearsing until they're boring.

## 11. Cheat sheet

| Thing | One-liner |
|---|---|
| Autonomy level | L1: agents never approve or promote |
| Agents | Investigator, Fixer, Verifier, Critic |
| Orchestration | LangGraph, with governance in plain Python outside it |
| Confidence | Computed from signal agreement, not self-reported by the model |
| Verifier | Code-first, model-free; checks the alert metric actually recovered |
| Promotion | Reserve → deploy → only Jenkins `SUCCESS` counts; approver ≠ promoter |
| Audit | Append-only, one entry per state change, tool calls included |
| Events | Pull-based bus (in-process / Postgres outbox / Kafka), at-least-once, idempotent consumers |
| Learning | Incident outcome: only `resolved` teaches; resolution written from facts |
| Trust-building | Shadow mode + agent outcome metrics + run budgets |
| Biggest unknown | Real-model behaviour: never run against a real LLM yet |
