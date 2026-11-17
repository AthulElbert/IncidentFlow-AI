"""Policy decisions: the production promotion gate, and who may act on which service's changes.

`PythonPolicy` is the reference and the default. `OpaPolicy` asks an Open Policy Agent server to evaluate the Rego
rules in `policy/`, which say the same thing; one table of cases checks the two agree (`tests/test_policy.py`).
Thresholds and service ownership are passed in each request's input, so both engines decide from the same settings
and the Rego holds only the logic.

Every decision names the engine and a hash of the rules that made it (`bundle`), and is written to the audit log
with the action it allowed or refused, so a past decision can be explained later.
"""
from app.policy.engine import (
    OpaPolicy,
    PolicyDecision,
    PolicyEngine,
    PolicyUnavailable,
    PythonPolicy,
    authz_input,
    promotion_input,
)
from app.policy.ownership import Ownership, load_ownership

__all__ = [
    "OpaPolicy",
    "Ownership",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyUnavailable",
    "PythonPolicy",
    "authz_input",
    "load_ownership",
    "promotion_input",
]
