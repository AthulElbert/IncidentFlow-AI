"""The policy engines: one table of cases that Python and OPA must decide alike, and OPA failing closed."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.models.schemas import ApprovalDecisionRequest, PromoteRequest
from app.policy import OpaPolicy, PolicyDecision, PolicyUnavailable, PythonPolicy, load_ownership
from app.services.change_control import ChangeControlStore, PolicyConfig

ROOT = Path(__file__).resolve().parents[1]
PARAMS = {"min_confidence": 0.8, "require_zero_warnings": True, "allowed_jenkins_states": ["SUCCESS"]}


def facts(**overrides):
    base = {
        "change_id": "CHG-1", "service": "payments-service", "environment": "prod", "confidence": 0.91,
        "warning_count": 0, "jenkins_status": "SUCCESS", "issue_type": "performance_degradation",
        "dev_execution_status": "passed", "params": dict(PARAMS),
    }
    base.update(overrides)
    return base


PROMOTION_CASES = [
    ("all good", facts(), []),
    ("low confidence", facts(confidence=0.655), ["confidence 0.66 below minimum 0.80"]),
    ("exactly the minimum passes", facts(confidence=0.8), []),
    ("warnings", facts(warning_count=2), ["warning_count 2 must be 0"]),
    ("warnings allowed", facts(warning_count=2, params={**PARAMS, "require_zero_warnings": False}), []),
    ("jenkins state, any case", facts(jenkins_status="success"), []),
    ("jenkins not success", facts(jenkins_status="FAILURE"), ["jenkins_status FAILURE not in allowed set SUCCESS"]),
    ("several allowed states, sorted", facts(jenkins_status="QUEUED", params={**PARAMS, "allowed_jenkins_states": ["UNSTABLE", "success"]}),
     ["jenkins_status QUEUED not in allowed set SUCCESS, UNSTABLE"]),
    ("unknown issue", facts(issue_type="unknown"), ["issue_type unknown requires manual deep-dive"]),
    ("no dev evidence", facts(dev_execution_status="failed"), ["dev execution evidence is not in passed state"]),
    ("everything wrong, in order", facts(confidence=0.1, warning_count=1, jenkins_status="ABORTED", issue_type="unknown", dev_execution_status="not_started"),
     ["confidence 0.10 below minimum 0.80", "warning_count 1 must be 0", "jenkins_status ABORTED not in allowed set SUCCESS",
      "issue_type unknown requires manual deep-dive", "dev execution evidence is not in passed state"]),
]


def who(actor="ana", teams=(), roles=("approver",)):
    return {"actor": actor, "roles": list(roles), "teams": list(teams)}


def authz(principal, owner="team-payments", admin="platform", service="payments-service", action="decide"):
    return {"principal": principal, "action": action, "service": service, "owner_team": owner, "admin_team": admin}


AUTHZ_CASES = [
    ("owning team", authz(who(teams=["team-payments"])), []),
    ("one of several teams", authz(who(teams=["x", "team-payments"])), []),
    ("admin team", authz(who(teams=["platform"])), []),
    ("other team", authz(who(teams=["team-orders"])), ["ana is not in team-payments, the team that owns payments-service"]),
    ("no team", authz(who()), ["ana is not in team-payments, the team that owns payments-service"]),
    ("unowned service", authz(who(teams=["team-payments"]), owner=None, service="ledger"),
     ["ledger has no owning team; only the admin team may act on it"]),
    ("unowned, admin", authz(who(teams=["platform"]), owner=None, service="ledger"), []),
    ("no admin team configured", authz(who(teams=[""]), admin=""), ["ana is not in team-payments, the team that owns payments-service"]),
]


def _engines():
    engines = [pytest.param(PythonPolicy(), id="python")]
    url = os.getenv("TEST_OPA_URL")
    engines.append(pytest.param(OpaPolicy(url) if url else None, id="opa",
                                marks=pytest.mark.skipif(not url, reason="TEST_OPA_URL not set (an OPA server with policy/ loaded)")))
    return engines


@pytest.mark.parametrize("engine", _engines())
@pytest.mark.parametrize("name,payload,reasons", PROMOTION_CASES, ids=[c[0] for c in PROMOTION_CASES])
def test_the_promotion_gate_decides_alike_in_every_engine(engine, name, payload, reasons):
    decision = engine.promotion(payload)
    assert list(decision.reasons) == reasons and decision.allow == (not reasons) and not decision.error


@pytest.mark.parametrize("engine", _engines())
@pytest.mark.parametrize("name,payload,reasons", AUTHZ_CASES, ids=[c[0] for c in AUTHZ_CASES])
def test_service_scoping_decides_alike_in_every_engine(engine, name, payload, reasons):
    decision = engine.authorize(payload)
    assert list(decision.reasons) == reasons and decision.allow == (not reasons)


def test_a_decision_names_its_engine_and_rules_for_the_audit_log():
    decision = PythonPolicy().promotion(facts(warning_count=1))
    assert decision.log() == {"engine": "python", "bundle": decision.bundle, "allow": False, "reasons": ["warning_count 1 must be 0"]}
    assert decision.bundle.startswith("python:") and len(decision.bundle) == len("python:") + 12


# --- OPA failing closed -------------------------------------------------------------------------------------------


class FakeOpa:
    """An HTTP server standing in for OPA, answering what each test tells it to."""

    def __init__(self, policies=None, decision=None, status=200):
        rego = "".join((ROOT / "policy" / name).read_text(encoding="utf-8") for name in ("promotion.rego", "authz.rego"))
        self.policies = policies if policies is not None else [{"id": "policy/all.rego", "raw": rego}]
        self.decision, self.status = decision, status
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"result": fake.policies})

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self._send(fake.status, fake.decision if fake.status == 200 else {"code": "internal_error"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_opa():
    servers = []

    def make(**kw):
        servers.append(FakeOpa(**kw))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def test_opa_reports_which_rules_it_runs(fake_opa):
    opa = fake_opa(decision={"result": {"allow": True, "reasons": []}})
    engine = OpaPolicy(opa.url)
    assert engine.bundle.startswith("opa:") and engine.promotion(facts()).allow
    opa.policies[0]["raw"] += "\n# changed"
    assert OpaPolicy(opa.url).bundle != engine.bundle


def test_opa_without_our_policies_or_unreachable_stops_the_app_starting(fake_opa):
    with pytest.raises(PolicyUnavailable, match="no policy for incidentflow/promotion"):
        OpaPolicy(fake_opa(policies=[{"id": "x", "raw": "package other"}]).url)
    with pytest.raises(PolicyUnavailable, match="unreachable"):
        OpaPolicy("http://127.0.0.1:9", timeout_seconds=0.5)


@pytest.mark.parametrize(
    "decision,status,expected",
    [
        (None, 500, "OPA returned 500"),
        ({}, 200, "gave no decision"),
        ({"result": {"allow": "yes", "reasons": []}}, 200, "gave no decision"),
        ({"result": {"allow": True, "reasons": ["but no"]}}, 200, "allowed and gave reasons"),
    ],
    ids=["server error", "policy not loaded", "not a boolean", "contradiction"],
)
def test_anything_but_a_clear_answer_from_opa_is_a_refusal_that_says_why(fake_opa, decision, status, expected):
    opa = fake_opa(decision={"result": {"allow": True, "reasons": []}})
    engine = OpaPolicy(opa.url)
    opa.decision, opa.status = decision, status
    result = engine.promotion(facts())
    assert result.allow is False and result.error is True and expected in result.reasons[0]
    assert result.reasons[0].startswith("policy engine unavailable")


def test_a_refusal_without_reasons_still_says_it_was_refused(fake_opa):
    opa = fake_opa(decision={"result": {"allow": False, "reasons": []}})
    assert OpaPolicy(opa.url).promotion(facts()).reasons == ("refused by policy (no reason given)",)


class Down:
    name, bundle = "opa", "opa:test"

    def promotion(self, facts):
        return PolicyDecision(False, ("policy engine unavailable: OPA unreachable",), self.name, self.bundle, error=True)

    authorize = promotion


def _ready_change(tmp_path, engine):
    store_file = tmp_path / "changes.json"
    store_file.write_text("[]", encoding="utf-8")
    store = ChangeControlStore(str(store_file), policy=PolicyConfig(), policy_engine=PythonPolicy())
    change = store.create_change(
        incident_id="INC-1", service="payments-service", environment="prod", summary="s", jira_key="SUP-1",
        jenkins_job_url="u", proposed_actions=[], triage_mode_used="heuristic", triage_hypothesis_steps=[],
        issue_type="performance_degradation", confidence=0.91, warning_count=0, jenkins_status="QUEUED",
    )
    store.record_dev_execution(change_id=change.change_id, executed_by="dev", execution_url="u", jenkins_status="SUCCESS",
                               apm_improvement_pct=12.0, smoke_tests_passed=True, validation_passed=True, notes="n")
    store.policy_engine = engine
    return store, change.change_id


def test_an_engine_outage_at_approval_decides_nothing_rather_than_blocking_the_change_for_good(tmp_path):
    store, change_id = _ready_change(tmp_path, Down())
    with pytest.raises(ValueError, match="Nothing was decided: policy engine unavailable"):
        store.apply_decision(change_id, ApprovalDecisionRequest(decision="approve"), "approver-1")
    assert store.get_change(change_id).status == "pending_approval"  # can be approved once the engine is back


def test_an_engine_outage_at_promotion_refuses_it(tmp_path):
    store, change_id = _ready_change(tmp_path, PythonPolicy())
    store.apply_decision(change_id, ApprovalDecisionRequest(decision="approve"), "approver-1")
    store.policy_engine = Down()
    with pytest.raises(ValueError, match="Promotion blocked by policy: policy engine unavailable"):
        store.begin_promotion(change_id, PromoteRequest(), "release-1")
    assert store.get_change(change_id).deployment_state == "ready_for_prod"


# --- ownership file -------------------------------------------------------------------------------------------------


def test_the_ownership_file_is_checked(tmp_path):
    path = tmp_path / "owners.json"
    path.write_text(json.dumps({"admin_team": "platform", "services": {"payments-service": "team-payments"}}), encoding="utf-8")
    owners = load_ownership(path)
    assert owners.owner("payments-service") == "team-payments" and owners.owner("ledger") is None
    assert load_ownership(ROOT / "ops" / "service_owners.example.json").admin_team == "platform"

    for bad, message in (
        ({"services": {"payments": "team payments"}}, "short name"),
        ({"services": ["payments"]}, "must map"),
        ({"owners": {}}, "nothing else"),
    ):
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_ownership(path)
    with pytest.raises(ValueError, match="does not exist"):
        load_ownership(tmp_path / "missing.json")
