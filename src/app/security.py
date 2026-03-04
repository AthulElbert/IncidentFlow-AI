from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, status
from jwt import InvalidTokenError


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    source: str


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

    def _auth_from_api_key(self, x_api_key: str | None) -> Principal | None:
        if not x_api_key:
            return None
        principal = self.key_registry.get(x_api_key)
        if not principal:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        return principal

    def _auth_from_bearer(self, authorization: str | None) -> Principal | None:
        if not authorization:
            return None
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header")

        token = authorization.split(" ", 1)[1].strip()
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

        return Principal(actor=actor, role=role, source="jwt")

    def authorize(self, required_roles: set[str]):
        def dependency(
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> Principal:
            if not self.enabled:
                return Principal(actor="auth-disabled", role="admin", source="disabled")

            mode = self.mode if self.mode in {"api_key", "jwt", "hybrid"} else "api_key"

            principal: Principal | None = None
            if mode in {"jwt", "hybrid"} and authorization:
                principal = self._auth_from_bearer(authorization)

            if principal is None and mode in {"api_key", "hybrid"}:
                principal = self._auth_from_api_key(x_api_key)

            if principal is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authentication credentials")

            if principal.role not in required_roles:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden for current role")

            return principal

        return dependency
