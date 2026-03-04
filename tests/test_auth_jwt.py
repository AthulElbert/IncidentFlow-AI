import importlib
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient



def _token(secret: str, role: str, actor: str = "user-1") -> str:
    payload = {
        "sub": actor,
        "role": role,
        "iss": "agentic-support",
        "aud": "agentic-support-api",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def test_jwt_auth_success(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-32-char-aaaaaaaaaaaaaaa")
    monkeypatch.setenv("JWT_ISSUER", "agentic-support")
    monkeypatch.setenv("JWT_AUDIENCE", "agentic-support-api")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    token = _token("jwt-secret-32-char-aaaaaaaaaaaaaaa", role="viewer", actor="jwt-viewer")
    r = client.get("/health", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    body = r.json()
    assert body["auth_role"] == "viewer"
    assert body["auth_source"] == "jwt"


def test_jwt_auth_invalid_token(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-32-char-aaaaaaaaaaaaaaa")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    bad = _token("wrong-secret-32-char-bbbbbbbbbbb", role="viewer")
    r = client.get("/health", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_jwt_role_forbidden(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-32-char-aaaaaaaaaaaaaaa")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    token = _token("jwt-secret-32-char-aaaaaaaaaaaaaaa", role="viewer")
    r = client.post(
        "/v1/changes/CHG-DOES-NOT-EXIST/promote",
        headers={"Authorization": f"Bearer {token}"},
        json={"comment": "x"},
    )
    assert r.status_code == 403


