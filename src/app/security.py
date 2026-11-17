"""Who is calling. API keys (service accounts and the demo), a shared-secret JWT, or OIDC tokens checked against the
issuer's published keys (JWKS).

A principal has roles (what kind of action it may take) and teams (whose services it may take them on; used when
AUTHZ_MODE=scoped). Roles are checked here; team scoping is a policy decision, made per change in `main`.
"""
import json
import logging
from dataclasses import dataclass, field
from urllib import request
from urllib.parse import urlparse

import jwt
from fastapi import Header, HTTPException, status
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

logger = logging.getLogger("app.security")

KNOWN_ROLES = ("viewer", "approver", "release_operator")
_ROLE_RANK = {role: rank for rank, role in enumerate(KNOWN_ROLES)}
OIDC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"})


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str  # the most privileged of `roles`, kept for display and older callers
    source: str
    roles: frozenset[str] = field(default_factory=frozenset)
    teams: frozenset[str] = field(default_factory=frozenset)

    @property
    def all_roles(self) -> frozenset[str]:
        return self.roles or frozenset({self.role})


def _claim_values(value) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return []


def principal_from_claims(claims: dict, source: str, actor_claim: str, roles_claim: str, teams_claim: str) -> Principal:
    actor = str(claims.get(actor_claim) or claims.get("sub") or "")
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no subject")
    # Only roles this application knows count; an identity provider's other groups are not roles here.
    roles = frozenset(r for r in _claim_values(claims.get(roles_claim)) if r in _ROLE_RANK)
    if not roles:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Token grants none of the roles {', '.join(KNOWN_ROLES)}")
    teams = frozenset(_claim_values(claims.get(teams_claim))) if teams_claim else frozenset()
    top = max(roles, key=lambda r: _ROLE_RANK[r])
    return Principal(actor=actor, role=top, source=source, roles=roles, teams=teams)


class OidcVerifier:
    """Checks tokens from an OpenID Connect provider: signature against its JWKS, issuer, audience, expiry.

    The JWKS location comes from OIDC_JWKS_URL or the issuer's discovery document. Keys are cached for five minutes;
    a token signed with a key id not in the cache makes one refetch (key rotation), at most once per
    `refresh_cooldown_seconds`, so tokens with made-up key ids cannot make every request call the provider. A newly
    rotated key may therefore be refused for up to that long. Symmetric algorithms are never accepted, so a token
    cannot be signed with the public key as an HMAC secret.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str = "",
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        actor_claim: str = "sub",
        roles_claim: str = "roles",
        teams_claim: str = "groups",
        leeway_seconds: int = 30,
        timeout_seconds: int = 5,
        refresh_cooldown_seconds: float = 30,
    ) -> None:
        unsafe = set(algorithms) - OIDC_ALGORITHMS
        if unsafe or not algorithms:
            raise ValueError(f"OIDC algorithms must be asymmetric ({', '.join(sorted(OIDC_ALGORITHMS))}), got {sorted(unsafe) or 'none'}")
        if not issuer or not audience:
            raise ValueError("OIDC needs an issuer and an audience")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.algorithms = list(algorithms)
        self.actor_claim, self.roles_claim, self.teams_claim = actor_claim, roles_claim, teams_claim
        self.leeway_seconds = leeway_seconds
        self.timeout_seconds = timeout_seconds
        self.refresh_cooldown_seconds = refresh_cooldown_seconds
        self._jwks_url = jwks_url
        self._client: PyJWKClient | None = None

    def _discover(self) -> str:
        with request.urlopen(f"{self.issuer}/.well-known/openid-configuration", timeout=self.timeout_seconds) as resp:
            config = json.loads(resp.read().decode("utf-8"))
        if config.get("issuer", "").rstrip("/") != self.issuer:
            raise ValueError("the discovery document names a different issuer")
        jwks_uri = config.get("jwks_uri", "")
        check_endpoint(jwks_uri, "jwks_uri")
        return jwks_uri

    def client(self) -> PyJWKClient:
        if self._client is None:
            url = self._jwks_url or self._discover()
            self._client = PyJWKClient(
                url, cache_keys=True, lifespan=300, timeout=self.timeout_seconds, cooldown_duration=self.refresh_cooldown_seconds
            )
        return self._client

    def verify(self, token: str) -> Principal:
        try:
            key = self.client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid bearer token: {exc}") from exc
        except (OSError, ValueError) as exc:  # the provider could not be reached or answered nonsense
            logger.warning("oidc-keys-unavailable", extra={"error": str(exc)[:200]})
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Identity provider keys unavailable") from exc
        return principal_from_claims(claims, "oidc", self.actor_claim, self.roles_claim, self.teams_claim)


def check_endpoint(url: str, what: str) -> None:
    """An identity endpoint must be https, except on this machine (for tests and local providers)."""
    parsed = urlparse(url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError(f"{what} must be an https URL (http only for localhost), got {url!r}")


class AuthManager:
    def __init__(
        self,
        enabled: bool,
        mode: str,
        key_registry: dict[str, Principal],
        jwt_secret: str,
        jwt_algorithm: str,
        jwt_issuer: str,
        jwt_audience: str,
        jwt_role_claim: str,
        jwt_actor_claim: str,
        jwt_teams_claim: str = "",
        oidc: OidcVerifier | None = None,
    ) -> None:
        self.enabled = enabled
        self.mode = mode
        self.key_registry = key_registry
        self.jwt_secret = jwt_secret
        self.jwt_algorithm = jwt_algorithm
        self.jwt_issuer = jwt_issuer
        self.jwt_audience = jwt_audience
        self.jwt_role_claim = jwt_role_claim
        self.jwt_actor_claim = jwt_actor_claim
        self.jwt_teams_claim = jwt_teams_claim
        self.oidc = oidc
        if mode == "oidc" and oidc is None:
            raise ValueError("AUTH_MODE=oidc needs an OIDC verifier")

    def _auth_from_api_key(self, x_api_key: str | None) -> Principal | None:
        if not x_api_key:
            return None
        principal = self.key_registry.get(x_api_key)
        if not principal:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        return principal

    def _bearer_token(self, authorization: str) -> str:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header")
        return authorization.split(" ", 1)[1].strip()

    def _auth_from_bearer(self, authorization: str | None) -> Principal | None:
        if not authorization:
            return None
        token = self._bearer_token(authorization)
        if self.mode == "oidc":
            return self.oidc.verify(token)
        try:
            claims = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=[self.jwt_algorithm],
                issuer=self.jwt_issuer,
                audience=self.jwt_audience,
            )
        except InvalidTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid bearer token: {exc}") from exc

        actor = str(claims.get(self.jwt_actor_claim) or claims.get("sub") or "unknown-actor")
        raw_role = claims.get(self.jwt_role_claim)
        if isinstance(raw_role, list) and raw_role:
            role = str(raw_role[0])
        else:
            role = str(raw_role or "")

        if not role:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="JWT role claim missing")
        teams = frozenset(_claim_values(claims.get(self.jwt_teams_claim))) if self.jwt_teams_claim else frozenset()
        return Principal(actor=actor, role=role, source="jwt", teams=teams)

    def authenticate(self, x_api_key: str | None, authorization: str | None) -> Principal:
        if not self.enabled:
            return Principal(actor="auth-disabled", role="admin", source="disabled")

        mode = self.mode if self.mode in {"api_key", "jwt", "hybrid", "oidc"} else "api_key"

        principal: Principal | None = None
        if mode in {"jwt", "hybrid", "oidc"} and authorization:
            principal = self._auth_from_bearer(authorization)

        # API keys stay available beside OIDC, for service accounts.
        if principal is None and mode in {"api_key", "hybrid", "oidc"}:
            principal = self._auth_from_api_key(x_api_key)

        if principal is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authentication credentials")
        return principal

    def authorize(self, required_roles: set[str]):
        def dependency(
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> Principal:
            principal = self.authenticate(x_api_key, authorization)
            if principal.source != "disabled" and not (principal.all_roles & required_roles):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden for current role")
            return principal

        return dependency
