"""The two policy engines and the inputs they decide on.

The reason texts are part of the contract: the dashboard shows them, and the parity test compares them word for word
between Python and Rego.
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib import request
from urllib.error import HTTPError, URLError

PROMOTION = "promotion"
AUTHZ = "authz"
_PACKAGES = {PROMOTION: "incidentflow/promotion", AUTHZ: "incidentflow/authz"}


class PolicyUnavailable(RuntimeError):
    """The policy engine could not decide. Callers treat this as a refusal and say so."""


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    reasons: tuple[str, ...]
    engine: str
    bundle: str
    error: bool = False  # the engine could not decide; `allow` is False and `reasons` says why

    def log(self) -> dict[str, Any]:
        """What the audit log keeps about a decision."""
        entry = {"engine": self.engine, "bundle": self.bundle, "allow": self.allow, "reasons": list(self.reasons)}
        if self.error:
            entry["error"] = True
        return entry


class PolicyEngine(Protocol):
    name: str
    bundle: str

    def promotion(self, facts: dict[str, Any]) -> PolicyDecision:
        """May this change go to production? Input from `promotion_input`."""
        ...

    def authorize(self, request: dict[str, Any]) -> PolicyDecision:
        """May this person act on this service's change? Input from `authz_input`."""
        ...


def promotion_input(record, policy_config) -> dict[str, Any]:
    return {
        "change_id": record.change_id,
        "service": record.service,
        "environment": record.environment,
        "confidence": record.confidence,
        "warning_count": record.warning_count,
        "jenkins_status": record.jenkins_status,
        "issue_type": record.issue_type,
        "dev_execution_status": record.dev_execution_status,
        "params": {
            "min_confidence": policy_config.min_confidence_for_prod,
            "require_zero_warnings": policy_config.require_zero_warnings_for_prod,
            "allowed_jenkins_states": list(policy_config.allowed_jenkins_states_for_prod),
        },
    }


def authz_input(principal, action: str, service: str, ownership) -> dict[str, Any]:
    return {
        "principal": {"actor": principal.actor, "roles": sorted(principal.all_roles), "teams": sorted(principal.teams)},
        "action": action,
        "service": service,
        "owner_team": ownership.owner(service),
        "admin_team": ownership.admin_team,
    }


def _promotion_reasons(facts: dict[str, Any]) -> list[str]:
    params = facts["params"]
    reasons: list[str] = []
    if facts["confidence"] < params["min_confidence"]:
        reasons.append(f"confidence {facts['confidence']:.2f} below minimum {params['min_confidence']:.2f}")
    if params["require_zero_warnings"] and facts["warning_count"] > 0:
        reasons.append(f"warning_count {facts['warning_count']} must be 0")
    allowed = sorted({state.upper() for state in params["allowed_jenkins_states"]})
    if facts["jenkins_status"].upper() not in allowed:
        reasons.append(f"jenkins_status {facts['jenkins_status']} not in allowed set {', '.join(allowed)}")
    if facts["issue_type"] == "unknown":
        reasons.append("issue_type unknown requires manual deep-dive")
    if facts["dev_execution_status"] != "passed":
        reasons.append("dev execution evidence is not in passed state")
    return reasons


def _authz_reasons(req: dict[str, Any]) -> list[str]:
    teams = set(req["principal"]["teams"])
    if req["admin_team"] and req["admin_team"] in teams:
        return []
    owner = req["owner_team"]
    if owner is not None and owner in teams:
        return []
    if owner is not None:
        return [f"{req['principal']['actor']} is not in {owner}, the team that owns {req['service']}"]
    return [f"{req['service']} has no owning team; only the admin team may act on it"]


class PythonPolicy:
    """The reference engine, in process. Its bundle is a hash of this file."""

    name = "python"

    def __init__(self) -> None:
        self.bundle = "python:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

    def _decide(self, reasons: list[str]) -> PolicyDecision:
        return PolicyDecision(allow=not reasons, reasons=tuple(reasons), engine=self.name, bundle=self.bundle)

    def promotion(self, facts):
        return self._decide(_promotion_reasons(facts))

    def authorize(self, request):
        return self._decide(_authz_reasons(request))


class OpaPolicy:
    """Asks an OPA server (`POST /v1/data/incidentflow/<package>`). Any failure is a refusal that says why.

    The bundle is a hash of the incidentflow policies the server reported at startup, so the audit log records which
    rules were loaded; a server reloaded with other rules afterwards is not noticed until the next start.
    """

    name = "opa"

    def __init__(self, url: str, timeout_seconds: float = 2.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("OPA_URL must be an http(s) URL")
        self.url = url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.bundle = "opa:" + self._loaded_policies_hash()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(f"{self.url}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:200] if exc.fp else ""
            raise PolicyUnavailable(f"OPA returned {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise PolicyUnavailable(f"OPA unreachable: {exc}") from exc

    def _loaded_policies_hash(self) -> str:
        policies = self._call("GET", "/v1/policies").get("result") or []
        ours = sorted((p.get("id", ""), p.get("raw", "")) for p in policies if "package incidentflow." in p.get("raw", ""))
        missing = [pkg for pkg in _PACKAGES.values() if not any(f"package {pkg.replace('/', '.')}" in raw for _, raw in ours)]
        if missing:
            raise PolicyUnavailable(f"OPA has no policy for {', '.join(missing)}; load the files in policy/")
        return hashlib.sha256(json.dumps(ours).encode("utf-8")).hexdigest()[:12]

    def _decide(self, kind: str, payload: dict[str, Any]) -> PolicyDecision:
        try:
            result = self._call("POST", f"/v1/data/{_PACKAGES[kind]}", {"input": payload}).get("result")
            if not isinstance(result, dict) or not isinstance(result.get("allow"), bool) or not isinstance(result.get("reasons"), list):
                raise PolicyUnavailable(f"OPA gave no decision for {_PACKAGES[kind]} (is the policy loaded?)")
            reasons = tuple(str(r) for r in result["reasons"])
            if result["allow"] and reasons:
                raise PolicyUnavailable("OPA allowed and gave reasons to refuse; treating it as a refusal")
            if not result["allow"] and not reasons:
                reasons = ("refused by policy (no reason given)",)
            return PolicyDecision(allow=result["allow"], reasons=reasons, engine=self.name, bundle=self.bundle)
        except PolicyUnavailable as exc:
            return PolicyDecision(
                allow=False, reasons=(f"policy engine unavailable: {exc}",), engine=self.name, bundle=self.bundle, error=True
            )

    def promotion(self, facts):
        return self._decide(PROMOTION, facts)

    def authorize(self, request):
        return self._decide(AUTHZ, request)
