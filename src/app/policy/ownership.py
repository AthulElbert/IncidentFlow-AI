"""Which team owns which service, for service-scoped authorisation (AUTHZ_MODE=scoped).

    {"admin_team": "platform", "services": {"payments-service": "team-payments", "checkout-service": "team-checkout"}}

A service not listed has no owner, and only the admin team may act on its changes: an unknown service fails closed.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/@")


@dataclass(frozen=True)
class Ownership:
    admin_team: str = ""
    services: dict[str, str] = field(default_factory=dict)

    def owner(self, service: str) -> str | None:
        return self.services.get(service)


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 100 or not set(value) <= _NAME_CHARS:
        raise ValueError(f"{what} must be a short name (letters, digits, - _ . : / @), got {value!r}")
    return value


def load_ownership(path: Path) -> Ownership:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{path} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {"admin_team", "services"}:
        raise ValueError('expected {"admin_team": "...", "services": {"<service>": "<team>"}} and nothing else')
    services = raw.get("services") or {}
    if not isinstance(services, dict):
        raise ValueError('"services" must map service names to team names')
    admin = raw.get("admin_team", "")
    return Ownership(
        admin_team=_name(admin, "admin_team") if admin else "",
        services={_name(svc, "service"): _name(team, f"team of {svc}") for svc, team in services.items()},
    )
