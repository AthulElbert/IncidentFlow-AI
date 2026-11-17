"""OIDC: tokens checked against the issuer's published keys, with a local stand-in for the identity provider.

The provider here is a small HTTP server serving a discovery document and a JWKS made from keys generated in the
test. A real identity provider is not covered.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from agent_harness import APPROVER, scenario_incident
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from app.config import ConfigError, load_settings, validate_settings
from app.security import OidcVerifier

AUDIENCE = "incidentflow-api"


class Provider:
    """Discovery document plus a JWKS that a test can rotate or take down."""

    def __init__(self):
        self.keys: dict[str, tuple[str, object]] = {}  # kid -> (alg, private key)
        self.down = False
        self.jwks_fetches = 0
        self.discovery_issuer = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if provider.down:
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path == "/.well-known/openid-configuration":
                    body = {"issuer": provider.discovery_issuer or provider.issuer, "jwks_uri": f"{provider.issuer}/jwks"}
                elif self.path == "/jwks":
                    provider.jwks_fetches += 1
                    body = {"keys": [provider.public_jwk(kid) for kid in provider.keys]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add_key(self, kid, alg="RS256"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if alg.startswith("RS") else ec.generate_private_key(ec.SECP256R1())
        self.keys[kid] = (alg, key)
        return key

    def public_jwk(self, kid):
        alg, key = self.keys[kid]
        jwk = (RSAAlgorithm if alg.startswith("RS") else ECAlgorithm).to_jwk(key.public_key(), as_dict=True)
        return {**jwk, "kid": kid, "alg": alg, "use": "sig"}

    def token(self, kid, **claims):
        alg, key = self.keys[kid]
        now = int(time.time())
        body = {"iss": self.issuer, "aud": AUDIENCE, "sub": "ana@example.com", "iat": now, "exp": now + 300,
                "roles": ["approver"], "groups": ["team-checkout"], **claims}
        return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm=alg, headers={"kid": kid})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider():
    p = Provider()
    p.add_key("k1")
    yield p
    p.close()


def verifier(provider, **kw):
    return OidcVerifier(issuer=provider.issuer, audience=AUDIENCE, **kw)


def _refused(v, token, status=401):
    with pytest.raises(HTTPException) as exc:
        v.verify(token)
    assert exc.value.status_code == status
    return exc.value.detail


def test_a_valid_token_becomes_a_principal_with_roles_and_teams(provider):
    principal = verifier(provider).verify(provider.token("k1", roles=["approver", "release_operator", "some-other-group"]))
    assert principal.actor == "ana@example.com" and principal.source == "oidc"
    assert principal.roles == {"approver", "release_operator"} and principal.role == "release_operator"
    assert principal.teams == {"team-checkout"}


def test_ec_keys_work_and_space_separated_claims_are_read(provider):
    provider.add_key("e1", "ES256")
    principal = verifier(provider).verify(provider.token("e1", roles="viewer approver", groups="a b"))
    assert principal.roles == {"viewer", "approver"} and principal.teams == {"a", "b"}


@pytest.mark.parametrize(
    "claims,message",
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "another-api"}, "Audience"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"exp": None}, "exp"),
        ({"roles": ["admin", "owner"]}, "none of the roles"),
    ],
    ids=["wrong issuer", "wrong audience", "expired", "no expiry", "no known role"],
)
def test_tokens_that_are_not_for_us_are_refused(provider, claims, message):
    assert message in _refused(verifier(provider), provider.token("k1", **claims))


def test_a_token_signed_by_another_key_is_refused(provider):
    other = Provider()
    try:
        other.add_key("k1")  # same key id, different key
        forged = jwt.encode({"iss": provider.issuer, "aud": AUDIENCE, "sub": "mallory", "exp": int(time.time()) + 300, "roles": ["release_operator"]},
                            other.keys["k1"][1], algorithm="RS256", headers={"kid": "k1"})
    finally:
        other.close()
    assert "Signature verification failed" in _refused(verifier(provider), forged)


def test_the_public_key_cannot_be_used_as_an_hmac_secret(provider):
    public_pem = provider.keys["k1"][1].public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    claims = {"iss": provider.issuer, "aud": AUDIENCE, "sub": "mallory", "exp": int(time.time()) + 300, "roles": ["release_operator"]}
    header = json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode()
    import base64
    import hashlib
    import hmac

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    signing_input = b64(header) + b"." + b64(json.dumps(claims).encode())
    token = (signing_input + b"." + b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())).decode()
    _refused(verifier(provider), token)
    with pytest.raises(ValueError, match="asymmetric"):
        OidcVerifier(issuer=provider.issuer, audience=AUDIENCE, algorithms=("HS256",))


def test_a_rotated_key_is_fetched_once_and_cached_keys_are_reused(provider):
    v = verifier(provider, refresh_cooldown_seconds=0)
    v.verify(provider.token("k1"))
    v.verify(provider.token("k1"))
    assert provider.jwks_fetches == 1
    provider.add_key("k2")  # the provider rotates: tokens now come signed with a key we have not seen
    assert v.verify(provider.token("k2")).actor == "ana@example.com"
    assert provider.jwks_fetches == 2
    # A key id the provider does not publish (a forged or stale token) is refused after one more look
    assert "Unable to find a signing key" in _refused(v, _unknown_kid_token(provider))


def test_tokens_with_made_up_key_ids_cannot_make_every_request_call_the_provider(provider):
    v = verifier(provider)  # the default cooldown
    v.verify(provider.token("k1"))
    for _ in range(5):
        assert "Unable to find a signing key" in _refused(v, _unknown_kid_token(provider))
    assert provider.jwks_fetches == 1  # within the cooldown, an unknown key id does not refetch


def _unknown_kid_token(provider):
    provider.add_key("gone")
    token = provider.token("gone")
    del provider.keys["gone"]
    return token


def test_an_unreachable_provider_is_a_503_not_a_login_failure(provider):
    provider.down = True
    assert _refused(verifier(provider), provider.token("k1"), status=503) == "Identity provider keys unavailable"


def test_discovery_must_name_the_same_issuer(provider):
    provider.discovery_issuer = "https://elsewhere.example"
    assert _refused(verifier(provider), provider.token("k1"), status=503)
    assert verifier(provider, jwks_url=f"{provider.issuer}/jwks").verify(provider.token("k1")).actor == "ana@example.com"


def test_oidc_settings_are_checked(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", "http://idp.example.com")
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.setenv("OIDC_ALGORITHMS", "RS256,HS256")
    with pytest.raises(ConfigError) as exc:
        validate_settings(load_settings())
    text = str(exc.value)
    assert "OIDC_AUDIENCE" in text and "must be an https URL" in text and "asymmetric" in text


def test_through_the_app_an_oidc_user_acts_and_api_keys_still_work_for_service_accounts(make_app, provider):
    _, client = make_app(AUTH_MODE="oidc", OIDC_ISSUER=provider.issuer, OIDC_AUDIENCE=AUDIENCE)
    bearer = {"Authorization": f"Bearer {provider.token('k1', sub='ana@example.com')}"}
    assert client.get("/v1/whoami", headers=bearer).json() == {
        "actor": "ana@example.com", "role": "approver", "roles": ["approver"], "teams": ["team-checkout"]
    }
    change_id = scenario_incident(client)  # with an API key
    decided = client.post(f"/v1/changes/{change_id}/decision", headers=bearer, json={"decision": "reject"})
    assert decided.status_code == 200 and decided.json()["decided_by"] == "ana@example.com"
    viewer_only = {"Authorization": f"Bearer {provider.token('k1', roles=['viewer'])}"}
    assert client.post(f"/v1/changes/{change_id}/investigate", headers=viewer_only).status_code == 403
    assert client.get("/v1/whoami", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    assert client.get("/v1/whoami", headers=APPROVER).json()["actor"] == "approver-user"


def test_oidc_and_scoping_together_use_the_tokens_groups(make_app, provider, tmp_path):
    owners = tmp_path / "owners.json"
    owners.write_text(json.dumps({"admin_team": "platform", "services": {"faulty-project": "team-checkout"}}), encoding="utf-8")
    _, client = make_app(AUTH_MODE="oidc", OIDC_ISSUER=provider.issuer, OIDC_AUDIENCE=AUDIENCE,
                         AUTHZ_MODE="scoped", SERVICE_OWNERSHIP_FILE=str(owners), APPROVER_TEAMS="platform")
    change_id = scenario_incident(client)
    outsider = {"Authorization": f"Bearer {provider.token('k1', sub='bo', groups=['team-orders'])}"}
    insider = {"Authorization": f"Bearer {provider.token('k1', sub='ana')}"}
    assert client.post(f"/v1/changes/{change_id}/decision", headers=outsider, json={"decision": "reject"}).status_code == 403
    assert client.post(f"/v1/changes/{change_id}/decision", headers=insider, json={"decision": "reject"}).status_code == 200
