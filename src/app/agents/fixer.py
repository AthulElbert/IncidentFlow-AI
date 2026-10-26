"""The Fixer: proposes a targeted change in an isolated worktree, using only the sandbox and memory tools.

It never runs tests, deploys, or decides that its own change works; the Verifier does that, and a
human approves. A model drives the loop. With no model it returns `no_fix` instead of inventing a
change: a templated "mitigation" file would only give the Verifier something meaningless to verify.
"""
import json
import logging
from typing import Any

from app.adapters.llm_client import LLMClient
from app.agents.evidence import clean_text
from app.agents.models import Evidence, FixAttempt, FixProposal, Hypothesis, IncidentContext
from app.agents.prompts import FIXER_FINAL_INSTRUCTION, FIXER_SYSTEM_PROMPT
from app.agents.verifier import changed_paths
from app.tools.base import ToolBudgetExceeded, ToolError, ToolNotPermitted
from app.tools.instrumentation import AgentTools

logger = logging.getLogger("app.fixer")

MAX_READ_CHARS = 12_000
MAX_LISTED_FILES = 200
MAX_GREP_MATCHES = 30
MAX_EVIDENCE_IN_PROMPT = 20
MAX_EDIT_CHARS = 200_000
MAX_TEXT = 1000


class Fixer:
    def __init__(
        self,
        llm: LLMClient | None = None,
        max_steps: int = 12,
        editable_paths: list[str] | None = None,
        max_changed_lines: int = 200,
    ) -> None:
        self.llm = llm
        self.max_steps = max(1, max_steps)
        self.editable_paths = list(editable_paths or [])
        self.max_changed_lines = max_changed_lines

    # ------------------------------------------------------------------ public

    def propose(
        self,
        ctx: IncidentContext,
        hypothesis: Hypothesis,
        evidence: list[Evidence],
        tools: AgentTools,
        branch: str,
        feedback: str = "",
        attempt: int = 1,
    ) -> FixAttempt:
        if self.llm is None:
            return FixAttempt(status="no_fix", reason="no model is configured for the Fixer, so it cannot propose a change")
        if "sandbox" not in tools.names:
            raise ValueError("the Fixer needs a sandbox to work in")

        messages: list[dict] = [
            {"role": "system", "content": FIXER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(self._task(ctx, hypothesis, evidence, feedback, attempt))},
        ]
        steps = 0
        final: dict[str, Any] | None = None
        warnings: list[str] = []

        try:
            # max_steps tool uses, then one more reply once the model has been told it must finish.
            while steps <= self.max_steps + 1:
                reply = self.llm.complete_json(messages, error_label="fixer")
                action = reply.get("action")
                if action == "final":
                    final = reply
                    break
                if action != "tool":
                    raise ValueError(f"unexpected action '{action}'")

                messages.append({"role": "assistant", "content": json.dumps(reply)})
                if steps >= self.max_steps:
                    messages.append({"role": "user", "content": json.dumps({"instruction": FIXER_FINAL_INSTRUCTION})})
                    steps += 1
                    continue
                steps += 1
                observation = self._run_tool(reply, tools, ctx.incident_id)
                payload: dict[str, Any] = {"observation": observation, "steps_remaining": self.max_steps - steps}
                if steps >= self.max_steps:
                    payload["instruction"] = FIXER_FINAL_INSTRUCTION
                messages.append({"role": "user", "content": json.dumps(payload)})
        except (ToolBudgetExceeded, ToolNotPermitted) as exc:
            return self._failed(f"tool access ended: {exc}", steps, tools)
        except Exception as exc:  # model errors, malformed replies
            logger.warning("fixer-failed", extra={"incident_id": ctx.incident_id, "error": str(exc)})
            return self._failed(clean_text(str(exc), 200), steps, tools)

        if final is None:
            return self._failed("the model did not give a final answer", steps, tools)

        summary = clean_text(str(final.get("summary", "")), MAX_TEXT)
        if final.get("no_fix"):
            return FixAttempt(
                status="no_fix",
                summary=summary,
                reason=clean_text(str(final.get("rationale") or "the model found no safe targeted change"), MAX_TEXT),
                steps=steps,
                tool_calls_used=tools.calls_used,
            )

        try:
            files = changed_paths(tools.sandbox.diff())
        except (ToolError, ToolBudgetExceeded) as exc:
            return self._failed(f"could not read back the changes: {clean_text(str(exc), 160)}", steps, tools)
        if not files:
            return FixAttempt(
                status="no_fix",
                summary=summary,
                reason="the model reported a fix but the worktree has no changes",
                steps=steps,
                tool_calls_used=tools.calls_used,
            )

        proposal = FixProposal(
            incident_id=ctx.incident_id,
            branch=branch,
            claimed_files=files,  # taken from the worktree, never from the model's say-so
            rationale=clean_text(str(final.get("rationale", "")), MAX_TEXT),
        )
        return FixAttempt(
            status="proposed",
            proposal=proposal,
            summary=summary,
            steps=steps,
            tool_calls_used=tools.calls_used,
            warnings=warnings,
        )

    # ----------------------------------------------------------------- internals

    def _failed(self, reason: str, steps: int, tools: AgentTools) -> FixAttempt:
        return FixAttempt(status="failed", reason=reason, steps=steps, tool_calls_used=tools.calls_used)

    def _task(
        self, ctx: IncidentContext, hypothesis: Hypothesis, evidence: list[Evidence], feedback: str, attempt: int
    ) -> dict[str, Any]:
        cited = [item for item in evidence if item.id in set(hypothesis.supporting)]
        context = (cited + [item for item in evidence if item not in cited])[:MAX_EVIDENCE_IN_PROMPT]
        task: dict[str, Any] = {
            "task": "Fix the cause of this incident with the smallest safe change.",
            "incident": {
                "service": ctx.service,
                "environment": ctx.environment,
                "alert_metric": ctx.alert_metric,
                "alert_message": clean_text(ctx.alert_message, 300),
                "value": ctx.value,
                "threshold": ctx.threshold,
            },
            "diagnosis": {
                "issue_type": hypothesis.issue_type,
                "root_cause": clean_text(hypothesis.root_cause, 500),
                "confidence": hypothesis.confidence,
            },
            "evidence": [{"id": item.id, "kind": item.kind, "summary": item.summary} for item in context],
            "editable_paths": self.editable_paths,
            "max_changed_lines": self.max_changed_lines,
            "attempt": attempt,
            "steps_available": self.max_steps,
        }
        if feedback:
            task["feedback"] = {
                "note": "Your previous attempt was checked and rejected. The worktree has been reset to the original code.",
                "failed_checks": clean_text(feedback, 2000),
            }
        return task

    def _run_tool(self, request: dict[str, Any], tools: AgentTools, ctx_incident_id: str = "") -> dict[str, Any]:
        tool, method = request.get("tool"), request.get("method")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        try:
            if (tool, method) == ("sandbox", "list_files"):
                files = tools.sandbox.list_files(str(args.get("subdir", "")))
                return {"files": files[:MAX_LISTED_FILES], "truncated": len(files) > MAX_LISTED_FILES}
            if (tool, method) == ("sandbox", "read"):
                text = tools.sandbox.read(str(args.get("path", "")))
                return {"path": str(args.get("path", "")), "content": text[:MAX_READ_CHARS], "truncated": len(text) > MAX_READ_CHARS}
            if (tool, method) == ("sandbox", "grep"):
                matches = tools.sandbox.grep(str(args.get("pattern", "")), str(args.get("subdir", "")), MAX_GREP_MATCHES)
                return {"matches": [{"path": m.path, "line": m.line, "text": clean_text(m.text, 200)} for m in matches]}
            if (tool, method) == ("sandbox", "edit"):
                content = args.get("content")
                if not isinstance(content, str) or len(content) > MAX_EDIT_CHARS:
                    return {"error": "edit needs 'path' and the complete new 'content' as a string"}
                result = tools.sandbox.edit(str(args.get("path", "")), content)
                return {"path": result.path, "lines": result.lines, "changed": result.changed}
            if (tool, method) == ("sandbox", "diff"):
                return {"diff": tools.sandbox.diff()[:MAX_READ_CHARS]}
            if (tool, method) == ("memory", "search"):
                found = tools.memory.search(clean_text(str(args.get("text", "")), 300), k=3, exclude_ids=[ctx_incident_id])
                return {
                    "matches": [
                        {"incident_id": m.incident_id, "issue_type": m.issue_type, "resolution": clean_text(m.resolution, 200)}
                        for m in found
                    ]
                }
        except ToolError as exc:
            return {"error": clean_text(str(exc), 300)}
        except ToolNotPermitted:
            return {"error": f"you may not use {tool}.{method}"}
        return {"error": f"unsupported request {tool}.{method}"}
