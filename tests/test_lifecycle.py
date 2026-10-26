import typing

import pytest

from app.models.schemas import ChangeRecord
from app.services.lifecycle import PRE_APPROVAL_STATES, TERMINAL_STATES, TRANSITIONS, assert_transition, is_allowed

ALL_STATES = set(typing.get_args(ChangeRecord.model_fields["deployment_state"].annotation))


def test_every_declared_state_is_known_to_the_lifecycle():
    known = set(TRANSITIONS) | {s for targets in TRANSITIONS.values() for s in targets}
    assert ALL_STATES <= known | TERMINAL_STATES
    # ...and the lifecycle does not invent states the model cannot hold.
    assert known <= ALL_STATES


@pytest.mark.parametrize(
    "source,target",
    [
        # the manual flow in use today
        ("awaiting_dev_execution", "dev_fix_in_progress"),
        ("awaiting_dev_execution", "ready_for_prod"),
        ("awaiting_dev_execution", "dev_fix_failed"),
        ("dev_fix_in_progress", "ready_for_prod"),
        ("dev_fix_in_progress", "dev_fix_failed"),
        ("dev_fix_failed", "ready_for_prod"),
        ("ready_for_prod", "blocked_by_policy"),
        ("ready_for_prod", "promoting"),
        ("promoting", "promoted_to_prod"),
        ("promoting", "prod_promotion_failed"),
        # agent workflow (reserved)
        ("awaiting_dev_execution", "investigating"),
        ("investigating", "hypothesis_ready"),
        ("verifying", "fixing"),
        ("reviewing", "ready_for_prod"),
        ("escalated", "investigating"),
        ("promoted_to_prod", "monitoring"),
        ("monitoring", "rolled_back"),
        # same state is always fine
        ("promoting", "promoting"),
        ("ready_for_prod", "ready_for_prod"),
    ],
)
def test_legal_transitions(source, target):
    assert is_allowed(source, target)
    assert_transition(source, target)


@pytest.mark.parametrize(
    "source,target",
    [
        ("awaiting_dev_execution", "promoting"),
        ("awaiting_dev_execution", "promoted_to_prod"),
        ("dev_fix_failed", "promoting"),
        ("ready_for_prod", "promoted_to_prod"),  # must pass through promoting
        ("promoting", "ready_for_prod"),
        ("promoted_to_prod", "ready_for_prod"),
        ("prod_promotion_failed", "promoting"),
        ("closed_rejected", "ready_for_prod"),
        ("blocked_by_policy", "promoting"),
        ("investigating", "promoting"),
        ("monitoring", "ready_for_prod"),
    ],
)
def test_illegal_transitions(source, target):
    assert not is_allowed(source, target)
    with pytest.raises(ValueError, match="Illegal state transition"):
        assert_transition(source, target)


def test_any_unapproved_state_can_be_rejected():
    for state in PRE_APPROVAL_STATES:
        assert is_allowed(state, "closed_rejected"), state


def test_nothing_leaves_a_terminal_state():
    for state in TERMINAL_STATES:
        assert TRANSITIONS.get(state, frozenset()) == frozenset(), state


def test_a_deploy_can_never_be_skipped():
    # The only way into promoted_to_prod is through promoting.
    sources = [s for s, targets in TRANSITIONS.items() if "promoted_to_prod" in targets]
    assert sources == ["promoting"]
