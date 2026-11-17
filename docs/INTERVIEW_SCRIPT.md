# IncidentFlow-AI: the complete interview script

Spoken answers in first person, ready to adapt. Each question has the answer, then the follow-ups an interviewer is likely to ask next, with answers. **Do not memorise word for word.** Learn the shape of each answer (the bold lead-in), then say it your own way.

Companion file: `docs/INTERVIEW_GUIDE.md` (the reasoning and the stories).

## 0. Three rules that make the rest work

1. **Anchor sentence.** *"The agents propose and prove. People decide. The platform makes sure nothing skips that."* If you lose the thread, say it and continue from it.
2. **Name the trade-off.** "I chose X because Y. The cost is Z."
3. **Say what isn't verified before you're asked.** It is what makes everything else believable. The list is in Part 8.

### Be ready for "how did you build it?"

Answer truthfully. Suggested wording, if it matches what happened:

> "I built it in phases with an AI coding assistant. I made the architecture decisions: four agents, human approval at L1 only, LangGraph for the loop with governance kept outside it, and Kafka-ready but not Kafka-first. I reviewed the code, I ran it end to end in a real browser against a real git repo, and I can walk you through any part of it."

Then make sure that last sentence is true: read the code for the parts in Part 3 before the interview. An interviewer who finds a gap between "I built it" and what you can explain will stop trusting the rest.

---

## Part 1. Openers

### 1.1 "Tell me about a project you're proud of." (30 seconds)

> "IncidentFlow is a production-support platform. When a monitoring alert fires, four AI agents investigate it, try a fix in an isolated copy of the code, verify the fix independently, and have a reviewer agent challenge it. A verified fix then waits for a person to approve it, and a *different* person has to release it. The interesting part isn't the AI, it's the governance around it: every state change is validated and audited, agents only get the tools their role needs, and it can run in shadow mode so you see what the agents would have done before you trust them with anything."

### 1.2 "Tell me more." (2 minutes)

> "**The problem:** alerts arrive faster than people can triage them, but a wrong automated fix in production costs far more than a slow human one. So I automate the investigation and the *proof*, not the decision.
>
> **The flow:** an alert opens a change record. The Investigator gathers evidence from metrics, logs, deploy history and similar past incidents. The Fixer edits code in a throwaway git worktree. The Verifier runs the tests, deploys to a dev environment and checks that the alert metric actually recovered. The Critic reviews the change. A verified fix then waits for an approver, and a different release operator promotes it.
>
> **Two decisions shaped everything:** agents never approve or release anything, and the safety rules live in plain tested Python *outside* the agent graph, so a model can't talk its way around them.
>
> **How I know it works:** about 600 automated tests, a replay harness, and I ran the full flow in a real browser, which found bugs the tests had missed."

### 1.3 "What was your role?"

> "I owned the design: what the agents may and may not do, where the human gates sit, and the trade-offs, such as LangGraph versus a workflow engine and Kafka-ready versus Kafka-first. I also drove it phase by phase and verified each phase by actually running it, not just trusting passing tests."

(Keep this honest. Adjust to what you actually did.)

---

## Part 2. The walkthrough (5 minutes, when they say "walk me through the architecture")

Draw as you speak: the change record in the middle, then the layers around it.

> "**Start with the spine.** Every incident becomes one *change record*. It holds the alert, the diagnosis, the fix and the decisions.
>
> **Around it is a gatekeeper.** Every write to a change goes through one layer that checks the move is legal, writes an append-only audit entry, and publishes an event. Nothing writes to the record any other way. That's where 'a change can't be approved before it's verified' is enforced.
>
> **Intake:** an alert arrives, a change is opened, and duplicate alerts for the same problem fold onto one change.
>
> **The agent loop** is a LangGraph graph: Investigator, then Fixer, then Verifier, then Critic, with a retry back to the Fixer, at most three attempts. The line I'd underline is: *the graph decides what happens next; it never decides what's allowed.* It can only change the record by calling the gatekeeper, which can refuse.
>
> **Each agent has its own job.** The Investigator collects evidence deterministically, and computes confidence from how many independent signals agree. It escalates instead of guessing if confidence is low or two explanations are too close. The Fixer is the only one that can edit, and only inside a disposable worktree, with protected files and a size limit. The Verifier doesn't use a model at all: it checks the diff, runs the tests, deploys to dev and confirms the alert metric recovered. The Critic checks scope and risk, and can only make the review stricter.
>
> **The run ends in one of two places:** a verified fix waiting for approval, or an escalation with the reason. There's no path from the agents to approve or release.
>
> **Then people.** An approver reviews the diagnosis, the diff and the checks, and approves. A different release operator promotes. Promotion reserves the change first, then triggers the deploy, and only a confirmed successful build counts.
>
> **Finally it learns.** When a change reaches production, an event goes out, a background consumer marks the incident resolved with a resolution written from facts, and that feeds the memory the Investigator uses next time. A postmortem draft is generated from the same records."

---

## Part 3. Question bank

Format: **Q**, the answer, then **Follow-ups**.

### A. Motivation and product

**Q: Why build this? Doesn't PagerDuty / Rootly / an incident tool already do this?**
> "Those tools mostly route and coordinate. They don't produce a *verified* fix. What I wanted to explore was the harder question: how do you let AI do real work on an incident while keeping the final decision, and the evidence for it, with people? The platform part, the governance, is the point, not the chatbot."

- *Follow-up: Who would use this?* "On-call engineers and the approvers who review fixes. The dashboard is built for the approver: it shows the diff that was verified, the checks, the risk and who may do what next."
- *Follow-up: What's the business value?* "Time to a *reviewable* fix, and fewer 2am investigations that end in 'nothing to see'. And, importantly, an audit trail for every decision."

**Q: Why not fully automate it?**
> "Because the costs are asymmetric. A wrong auto-release in production is expensive and trust-destroying; a slow human decision is cheap. I'd rather earn autonomy with evidence, which is what shadow mode is for."

- *Follow-up: So when would you allow more autonomy?* "After weeks of shadow data on a low-risk service, showing the agents' proposals matched what people did, with a low override rate. Even then I'd start with one service, with the human gate still on."

### B. Architecture

**Q: Why four agents instead of one?**
> "A single agent marks its own homework. Splitting the roles gives independent checks: the thing that writes a fix isn't the thing that judges it. The Verifier is also model-free, so a model's mistakes can't pass through a model's review."

- *Follow-up: Isn't that over-engineered?* "It would be for a toy. The cost is more moving parts and latency. I accepted that because each split removes a specific failure: fixing and verifying separately prevents self-approval, and the Critic catches scope creep the tests won't see."
- *Follow-up: Why a separate Critic if the Verifier already tests?* "Tests tell you it works, not whether the change is *appropriate*. The Critic checks that the files changed match what was claimed, that the change is small, that it's related to the evidence, and flags risk."

**Q: Why LangGraph? Why not Camunda, Step Functions or Bedrock Agents?**
> "The loop is a cycle with model-driven branching and retries, which LangGraph models directly. A workflow engine like Camunda is built for deterministic business processes. Bedrock would tie me to one cloud and one model, and I wanted the control logic to be mine. And I kept governance outside the graph on purpose, so swapping the orchestrator wouldn't touch the safety rules."

- *Follow-up: Is LangGraph doing anything you couldn't do with a loop?* "No. A while loop would work. LangGraph gives me explicit state, conditional routing and a place to add checkpointing, and it reads clearly. I'd say that plainly; I'm not selling the library."
- *Follow-up: What about its checkpointing?* "Progress is saved after every stage, in SQLite or Postgres. On restart an interrupted run resumes, but the unit of resumption is the fix attempt: if it died after the fix, it redoes that attempt from clean code, because I won't verify a worktree this process didn't produce. Resumes are capped and the budget carries over. I tested it by force-killing the app mid-fix and restarting it. I've run the Postgres checkpointer against a real database too. One gap: usage from the stage that was interrupted isn't counted."

**Q: What's the data model? How is state stored?**
> "One change record per incident, stored as JSON or Postgres JSONB behind the same interface. Every record has a version number; writes are conditional on it. Alongside are an append-only audit log, an event bus, and an incident memory store."

- *Follow-up: Why JSON as a backend at all?* "So it runs with zero setup for a demo, with atomic writes. Postgres is the real backend, with a transactional outbox."
- *Follow-up: Have you run it on real Postgres?* "Yes, against Postgres 16 in Docker: concurrent writers never lose an update, the audit log's immutability trigger, the outbox bus, and a run killed mid-way that resumes from the Postgres checkpoint. Those tests are gated by an environment variable so CI runs without a database."

**Q: How is the lifecycle enforced?**
> "A transition table: each state lists which states may follow. It's checked inside the single write path, so there's no way to skip it. An agent run can only set a small set of states, and only one method can move a change to ready-for-approval."

- *Follow-up: What if two things try to update the same change?* "Optimistic locking: the update only applies if the version hasn't moved; otherwise it retries, and the API returns 409."

### C. The AI and the agents

**Q: How do you stop the model doing something dangerous?**
> "In layers, none of which depends on the model behaving. Agents only hold the tools their role needs, through proxies that count and audit every call. Only the Fixer can edit, only inside a disposable git worktree, with an allowed-paths list, protected files and a line limit. No agent can approve or release. The lifecycle rejects illegal state changes. A person approves and a different person releases."

- *Follow-up: Is that a real sandbox?* "No, and I say so. It's an in-process boundary that stops accidents and bypasses by well-behaved code. It isn't a defence against hostile code in the same process. For that I'd run agents in containers."
- *Follow-up: What stops the Fixer editing the tests so they pass?* "Scope and protected-file rules, plus the Verifier checking that the files changed match the diff, and the Critic flagging unrelated or test-file changes. I won't claim that's airtight; a determined bad fix is exactly why a person still reviews the diff."

**Q: How do you handle hallucination?**
> "I don't trust model output directly. Confidence is computed by code from signals actually observed. The Fixer's list of changed files comes from the real diff, not from what it says it did. The Verifier re-checks with code. If a model proposes an invalid action or cites evidence that doesn't exist, it's overruled and flagged."

- *Follow-up: How is confidence calculated?* "Evidence items get tags assigned by code from the data: a metric anomaly, a deploy right before onset, a matching log pattern, a similar past incident. Confidence rises with the number of independent signals that agree and falls when signals contradict. It escalates if confidence is under 0.60, if the diagnosis is unknown, or if the top two explanations are within 0.10 of each other."
- *Follow-up: Why not ask the model for a confidence score?* "Models are poorly calibrated about themselves. A number a model makes up looks precise and means little."
- *Follow-up: Then what's the model for?* "Optional extras: asking for more evidence within a step limit, and wording the root cause. The Fixer needs a model, because writing a change is the one thing code can't do. Without a model it declines rather than inventing something."

**Q: What about prompt injection?**
> "I treat logs, deploy notes and ticket text as untrusted data. There's a built-in scenario with an instruction hidden in a log line. The agents' evidence tags are assigned by code, tool arguments are parsed into typed requests rather than passed through, and the UI labels evidence as untrusted. And even if a model were fooled, it still can't approve, release, or reach tools outside its role."

- *Follow-up: Can it be defeated?* "A determined attack on the model's wording of a root cause is possible, which is why a person reads the diff and evidence before approving. The design assumes the model can be fooled and limits what a fooled model can do."

**Q: What if the verification passes but the fix is wrong?**
> "That's the real residual risk. The Verifier checks that tests pass and that the alert metric recovered in dev, which is strong evidence but not proof. It's one reason there's still a human gate, a Critic's risk rating, and shadow mode to measure how often the agents are wrong."

- *Follow-up: How would you detect it after release?* "Honestly, that's a gap: there's no automatic post-release monitoring or rollback driven by the agents. I'd add a soak check and an automatic rollback proposal, with a person still deciding."

**Q: What does the Fixer actually do? How does it avoid unbounded loops?**
> "It runs a tool loop with a step limit: list files, read, search, edit, diff. It must finish with a summary or say there's no safe fix. Then bounded retries: if verification fails or the Critic sends it back, it retries on a reset worktree with the failing checks as feedback, at most three attempts. If verification is *inconclusive*, a Critic blocks, or there's no fix, it escalates immediately, because retrying can't create missing data."

### D. Governance and security

**Q: Explain separation of duties.**
> "The person who approves can't be the person who promotes. It's enforced on the server when a promotion is requested. The UI mirrors it, so the promote button is disabled with a reason if the release key belongs to the approver, but the server is the authority."

- *Follow-up: How does auth work?* "Three roles: viewer, approver, release operator, via API keys or JWT. Honestly that's demo-grade: for real use I'd put SSO in front."
- *Follow-up: What if auth is off?* "Then every request is the same synthetic actor, so a two-person rule can't be enforced, so it's switched off in that case and the system is meant for local use only."

**Q: Walk me through promotion safety.**
> "The original flow could mark a change promoted while the build was still queued. I changed it to *reserve first*: validate and move the change to 'promoting' atomically, then trigger the deploy. A retry or double-click is refused. Only a confirmed successful Jenkins build counts as promoted, and configuration that allows queued or running states for production is rejected at startup."

- *Follow-up: What if Jenkins goes down mid-deploy?* "The change stays in 'promoting' and shows that. A person presses refresh to resolve it. I haven't built a background poller, and I'd call that out as a gap."

**Q: What's in your audit log?**
> "Every state change has one entry: who, what, from and to state, details. Agent tool calls are audited too, under the agent's name, including denied attempts. In Postgres the log is append-only, enforced by a trigger. It's what the timeline and postmortem draw on."

### E. Reliability and concurrency

**Q: How do you handle duplicate alerts and double clicks?**
> "Three mechanisms. Alerts are deduplicated and correlated onto an existing open change within a time window. Starting an agent run and starting a promotion are both *reservations* that the second request fails. And event consumers are idempotent, because the bus delivers at least once."

**Q: What does at-least-once delivery mean for you?**
> "An event can arrive twice, so a consumer must give the same result the second time. My outcome learner checks whether the incident is still open before updating it, and it also sweeps the change store at startup, since the in-memory bus doesn't survive a restart."

**Q: What happens when the app restarts mid-run?**
> "With a durable checkpointer it resumes from the last completed stage, redoing the interrupted fix attempt from clean code, at most twice. Without one, or past the cap, it's escalated at startup with the reason, so nothing is left looking 'running'."

### F. Eventing and Kafka

**Q: Why is there an event bus at all?**
> "So parts of the system react to changes without being wired together: the learning consumer, the dashboard feed, and in future notifications. The interface is pull-based with explicit offset commits, which maps directly onto Kafka consumer groups."

- *Follow-up: Do you actually use Kafka?* "There's an adapter and it's optional. I'll be straight: I've only tested it against an in-memory stand-in for the client, so the mapping of keys, offsets and redelivery is covered, but rebalances, auth, TLS and real broker failures are not."
- *Follow-up: Why not Kafka from the start?* "It adds an operational dependency that a demo doesn't earn. Designing to the interface first meant I could add it without touching the consumers."
- *Follow-up: How do you avoid losing an event between the database and Kafka?* "That's the transactional outbox pattern. With Postgres the event row is written in the same transaction as the state change, and a relay forwards it to Kafka, committing only after Kafka confirms. Publishing straight to Kafka after the commit could lose an event."

### G. Testing and quality

**Q: How did you test an AI system?**
> "By separating what's deterministic from what isn't. Most of the system, including governance, confidence scoring and verification, is deterministic and fully testable. For the model I used a scripted stand-in that replays canned replies, so the loop's behaviour, including bad outputs, is testable. About 600 tests, hermetic against throwaway data."

- *Follow-up: How do you know the Investigator is accurate?* "I don't have a real-world accuracy number. I have a replay harness over seven scenarios that scores it against the old keyword classifier, but the scenarios and the rules were written together, so it's a regression check, not an accuracy estimate."
- *Follow-up: Have you run a real LLM through it?* "Yes, Gemini, on the free tier. The full flow produced a correct, verified fix, and on the prompt-injection scenario the model wasn't swayed and the Fixer declined rather than inventing a change. The surprises were not in the prompts: provider overload, rate limits and a daily quota, then malformed replies and arguments in the wrong place. I fixed each: honour the provider's retry delay, retry malformed replies, parse leniently but validate strictly. Three runs on a small model is a smoke test, not an evaluation."
- *Follow-up: What's a bug the tests wouldn't have found?* "The Investigator counted the incident it was investigating as past evidence, inflating its confidence. Only a real end-to-end run in a browser showed it, because the mocks matched my assumptions."
- *Follow-up: How do you test the UI?* "There are no JavaScript tests, which I'd call a gap. What I do have is a test that every button's enabled state agrees with what the server accepts at every stage, plus a check that every element the page wires up exists, because a missing id once blanked the dashboard."

### H. Evaluation and trust

**Q: How would you know the agents are good enough to trust more?**
> "Not from one number. I track: how often a run ends in a verified fix, how often it escalates, and the human override rate, meaning of verified fixes a person decided on, how many they rejected. Shadow mode adds a comparison with what people actually did. I'd want weeks of that on a low-risk service first."

- *Follow-up: What's shadow mode exactly?* "The agents run on every new incident and record what they *would* have done, but cannot affect the change: not its state, issue type, evidence or the approval gate. A report sets their proposals against what people did."
- *Follow-up: Is that a fair comparison?* "It's a proxy, and the report says so. Humans never see the proposals, so I compare the files the agents would have changed with the files a person released, and the agents' diagnosis with the keyword triage. It's evidence, not a score."

### I. Cost, scale and production

**Q: How do you control cost and runaway loops?**
> "Per-run budgets: wall-clock time, total tool calls across all agents, and model calls. Plus per-agent tool caps and a maximum of three attempts. Exceeding any limit escalates with the reason instead of looping. In shadow mode each incident costs a run, so I'd rate-limit that."

- *Follow-up: Do you count tokens?* "Yes, when the provider reports usage: tokens and cost per call go onto the run and into the audit log, and there are token and cost limits. If a provider doesn't report usage, the record says so and only the call limit applies."
- *Follow-up: What if your model provider goes down?* "Calls go through a router: a transient error is retried with backoff, then the next endpoint in that task's list is tried, and a circuit breaker skips an endpoint that keeps failing. I tested it end to end with a dead first-choice endpoint. I haven't run it against a real provider outage."

**Q: How would you take this to production?**
> "In order: run it against a real model; swap the mock backends for real ones, since metrics, logs, Jenkins and Jira are all behind interfaces; use Postgres and the outbox; put SSO in front; add a background job to track deploys; put the agents in containers; then run shadow mode for a few weeks and only then consider enabling real runs for one low-risk service."

**Q: How would it scale?**
> "Agent runs go on a small worker pool, and each run is isolated by its own worktree and budget. The store uses optimistic versioning, so multiple instances are safe with Postgres. The bottleneck would be model latency and dev deploys, not the platform. I haven't load-tested it."

### J. Judgement, weaknesses and reflection

**Q: What would you do differently?**
> "Run against a real model much earlier. When I finally did, the first failures weren't prompts at all: overload, rate limits, a daily quota, and replies in slightly the wrong shape. All cheap to handle, but I'd have designed for them from day one."

**Q: What's the biggest weakness?**
> "Three, in order of importance: no real model run; the Kafka path and real Postgres have not met real infrastructure; and in-flight run state isn't durable. The design handles all three, but I haven't proved it."

**Q: What was the hardest problem?**
> "Deciding what the AI is *not* allowed to do and making that enforceable rather than a convention. The agent code was the smaller part. The lifecycle table, the single write path, least-privilege tools and the human gates were the hard design."

**Q: Tell me about a bug you're glad you found.**
> Use the memory story: "Every new incident was saved with a copy of the most similar past incident's resolution, so a resolution could be inherited down a chain of incidents nobody had resolved, and then offered as evidence. I fixed it by giving each incident an outcome: only 'resolved' ones teach, and the resolution is written from facts, not by a model."

**Q: Tell me about a time something you built was wrong.**
> "My new shared tool budget let a refused call still spend one of the run's shared calls. A test I wrote for the edge showed it. I fixed the order, checking the agent's own cap first and refunding on refusal. The lesson was to test the boundary, not only the happy path."

---

## Part 4. Curveball questions

| Question | Answer shape |
|---|---|
| "Isn't this just an LLM wrapper?" | "The LLM is the least of it. Most of the code is governance: lifecycle, audit, least-privilege tools, a model-free verifier, concurrency control. Remove the model and the platform still works, it just can't propose a fix." |
| "What if the model is down?" | "The Investigator is deterministic, so it still produces a diagnosis. The Fixer has nothing to work with and declines, so the change escalates with a diagnosis. Nothing is invented." |
| "What if the alert metric recovers by itself?" | "Then the Verifier could be fooled. It's why it compares before and after with a minimum improvement, and why a person still looks. I'd add a control comparison as the next hardening step." |
| "How do you stop it being gamed by whoever writes the logs?" | "I don't trust logs: they're evidence, labelled untrusted, and tags that drive confidence come from code." |
| "Why Python/FastAPI?" | "Fast to build, strong typing with Pydantic, and the ecosystem for LangGraph. The architecture doesn't depend on it." |
| "Why JSON files?" | "A zero-setup demo backend behind the same interface as Postgres, with atomic writes. It's not for production." |
| "Show me the part you're least sure about." | Go straight to Part 8. Candour wins here. |
| "How long did it take?" | Answer truthfully; don't inflate. |
| "Could this go wrong in production?" | "Yes. The residual risk is a wrong fix that passes verification. The mitigations are the human gate, the Critic's risk rating, shadow mode, and no autonomy beyond L1." |

---

## Part 5. Behavioural stories (situation, what I found, what I changed, what I learned)

1. **Real run vs unit tests** (three defects found only by running it for real). *Learned:* mocks agree with your assumptions.
2. **Self-poisoning memory** (resolution copied down a chain). *Learned:* a learning loop needs ground truth.
3. **Deploy marked successful too early** (reserve-then-call). *Learned:* never let the success path be the default.
4. **`lstrip("./")` turning `.env` into `env`**. *Learned:* security checks fail in boring ways.
5. **My own budget bug caught by a test**. *Learned:* test the edge.
6. **Tests that wrote to tracked data files**. *Learned:* hermetic test fixtures are infrastructure, not polish.

---

## Part 6. Questions to ask the interviewer

Pick two or three that fit:
- "How do your on-call engineers triage today, and where does the time actually go?"
- "What would it take for your team to trust an automated fix? Is it evidence, an audit trail, or a track record?"
- "How do you decide today who may approve and who may release? Is it two people?"
- "Which parts of your incident process are already measurable enough to use as a shadow-mode baseline?"
- "How do you handle changes made by automation in your audit and compliance process?"

---

## Part 7. Demo script (3 minutes)

Run with `AGENT_RUN_MODE=manual` and the local stand-in model. Narrate what each step protects.

1. Open the dashboard. *"The list updates on its own."*
2. Create the `bad_config_latency` incident. *"A config deploy that tripled latency."*
3. Run Agents. *"Investigator gathers evidence; Fixer edits in a throwaway worktree."*
4. Open the approval screen: diagnosis, evidence, diff, checks, before/after metric. *"The reviewer sees exactly what was verified."*
5. Show Approve disabled until the box is ticked. *"Approving deploys nothing."*
6. Approve, then show the promotion screen. *"The release key has to belong to a different person."*
7. Promote; show the timeline and the postmortem draft. *"Every step is audited; the postmortem is built from facts."*
8. Close: *"Before enabling any of this for real, I'd run it in shadow mode and compare."*

**If it breaks:** switch to a finished change's review screen. It tells the same story. Say what you think broke; don't hide it.

---

## Part 8. What is not done (say it first)

- **Only a few real-model runs** (Gemini flash-lite, three full runs on my own scenarios): a smoke test, not an evaluation.
- **Kafka only against an in-memory stand-in.**
- **Usage spent by an interrupted stage isn't counted** after a resume.
- **No background Jenkins polling;** promotions need a manual refresh.
- **No automatic post-release monitoring or rollback.**
- **No JavaScript tests;** auth is API-key/JWT, not SSO; agents are not containerised.
- **Demo-level:** mock backends for metrics, logs, Jenkins, Jira. The real interfaces exist.

A line to use: *"I'd rather tell you what I haven't verified than have you find it."*

---

## Part 9. Words to avoid, and recovery lines

| Avoid | Say instead |
|---|---|
| "Fully autonomous" | "Agents propose and prove; people decide" |
| "The AI decides" | "The agents recommend; the gate decides" |
| "It's secure" | "These layers limit what a wrong or fooled model can do" |
| "It's accurate" | "It passes its regression scenarios; I don't have a real-world accuracy number" |
| "Production-ready" | "Production-shaped: real interfaces, demo-level backends" |

**If you blank:** "Let me think about that for a second." Then use the anchor sentence and answer from it.
**If you don't know:** "I haven't tested that. Here's how I would." Then say how.
**If you're challenged and they're right:** "That's fair. That's a real gap, and here's what I'd do about it." Concede cleanly. It reads as senior.

---

## Part 10. One-page cheat sheet

| Topic | Say |
|---|---|
| Autonomy | L1: agents never approve or release |
| Agents | Investigator, Fixer, Verifier, Critic |
| Orchestration | LangGraph; governance in plain Python outside it |
| Confidence | Computed from agreeing signals; escalates under 0.60 or when two causes are within 0.10 |
| Verifier | Model-free; diff, scope, syntax, tests, dev deploy, alert metric recovered, side effects |
| Retries | Max 3 attempts; inconclusive or blocked escalates immediately |
| Promotion | Reserve, then deploy; only confirmed `SUCCESS` counts; approver ≠ promoter |
| Concurrency | Optimistic versioning (409 on conflict); reservations; idempotent consumers |
| Audit | Append-only; tool calls included, denied ones too |
| Events | Pull-based; in-process, Postgres outbox, Kafka adapter |
| Learning | Only `resolved` incidents teach; resolution written from facts |
| Trust | Shadow mode, override rate, run budgets |
| Budgets | 900s, 150 tool calls, 60 model calls, 600k tokens per run (configurable); optional cost limit |
| Model routing | Per-task profiles, retry, fallback, circuit breaker; keys by env var; no real provider tested |
| Tests | ~600, hermetic; replay harness is a regression check, not an accuracy estimate |
| Biggest unknown | Real incidents: only a few real-model runs, on scenarios I wrote |
