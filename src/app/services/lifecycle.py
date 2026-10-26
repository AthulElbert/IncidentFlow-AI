"""Allowed `deployment_state` transitions for a change.

Every state change goes through ChangeControlStore._mutate, which rejects any
move not listed here. The states from `investigating` to `rolled_back` are the
agent workflow and post-deploy monitoring from docs/ARCHITECTURE.md; they are
declared now so the table is the single source of truth, but nothing enters them
yet. A change is "awaiting approval" when it is `ready_for_prod` with status
`pending_approval`.
"""

# States a change can be in while its approval status is still pending.
PRE_APPROVAL_STATES = frozenset(
    {
        "awaiting_dev_execution",
        "dev_fix_in_progress",
        "dev_fix_failed",
        "ready_for_prod",
        "investigating",
        "hypothesis_ready",
        "fixing",
        "verifying",
        "reviewing",
        "escalated",
    }
)

_EDGES: dict[str, frozenset[str]] = {
    # Current flow: manual dev run, then approval, then promotion.
    "awaiting_dev_execution": frozenset({"dev_fix_in_progress", "ready_for_prod", "dev_fix_failed", "investigating"}),
    "dev_fix_in_progress": frozenset({"ready_for_prod", "dev_fix_failed"}),
    "dev_fix_failed": frozenset({"dev_fix_in_progress", "ready_for_prod"}),
    "ready_for_prod": frozenset({"dev_fix_in_progress", "dev_fix_failed", "blocked_by_policy", "promoting"}),
    "promoting": frozenset({"promoted_to_prod", "prod_promotion_failed"}),
    # Agent workflow (reserved).
    "investigating": frozenset({"hypothesis_ready", "escalated"}),
    "hypothesis_ready": frozenset({"fixing", "escalated"}),
    "fixing": frozenset({"verifying", "escalated"}),
    "verifying": frozenset({"reviewing", "fixing", "escalated"}),
    "reviewing": frozenset({"ready_for_prod", "fixing", "escalated"}),
    "escalated": frozenset({"investigating"}),
    # Post-deploy monitoring (reserved).
    "promoted_to_prod": frozenset({"monitoring"}),
    "monitoring": frozenset({"resolved", "rolled_back"}),
}

TRANSITIONS: dict[str, frozenset[str]] = {
    state: edges | ({"closed_rejected"} if state in PRE_APPROVAL_STATES else frozenset())
    for state, edges in {**{s: frozenset() for s in PRE_APPROVAL_STATES}, **_EDGES}.items()
}

# Nothing leaves these.
TERMINAL_STATES = frozenset(
    {"closed_rejected", "blocked_by_policy", "prod_promotion_failed", "resolved", "rolled_back"}
)


def is_allowed(from_state: str, to_state: str) -> bool:
    return from_state == to_state or to_state in TRANSITIONS.get(from_state, frozenset())


def assert_transition(from_state: str, to_state: str) -> None:
    if not is_allowed(from_state, to_state):
        raise ValueError(f"Illegal state transition {from_state} -> {to_state}")
