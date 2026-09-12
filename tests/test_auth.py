"""Unit tests for Robinhood authentication, JWT sessions, and endpoint protection."""

import pytest
from fastapi.testclient import TestClient
from src.backend.app import app
from src.backend.auth import create_session_jwt, decode_session_jwt
from src.backend.config import settings


@pytest.fixture
def client():
    return TestClient(app)


def test_session_jwt_encode_decode():
    user_data = {"username": "trader@example.com", "access_token": "token123"}
    token = create_session_jwt(user_data)
    assert token is not None

    decoded = decode_session_jwt(token)
    assert decoded is not None
    assert decoded["username"] == "trader@example.com"
    assert decoded["access_token"] == "token123"


def test_invalid_session_jwt_returns_none():
    assert decode_session_jwt("invalid.token.here") is None


def test_public_health_endpoint_allowed_without_auth(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_login_page_accessible_without_auth(client):
    res = client.get("/login")
    assert res.status_code == 200
    assert "Master Passcode" in res.text


def test_login_with_valid_master_password(client):
    res = client.post("/api/auth/login", json={"password": settings.dashboard_password})
    assert res.status_code == 200
    assert res.json()["status"] == "success"
    assert "session_token" in res.cookies


def test_login_with_invalid_master_password(client):
    res = client.post("/api/auth/login", json={"password": "wrong-password-123"})
    assert res.status_code == 401
    assert "Incorrect master password" in res.json()["detail"]
    assert "session_token" not in res.cookies



def test_root_dashboard_redirects_unauthenticated_user_to_login(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login"


def test_protected_api_blocks_unauthenticated_request(client):
    res = client.get("/api/portfolio")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]


def test_protected_api_allows_authenticated_request(client):
    user_data = {"username": "trader@example.com", "access_token": "token123"}
    token = create_session_jwt(user_data)

    client.cookies.set("session_token", token)
    res = client.get("/api/portfolio")
    assert res.status_code == 200
    assert "total_equity" in res.json()


def test_logout_endpoint_clears_cookie(client):
    user_data = {"username": "trader@example.com"}
    token = create_session_jwt(user_data)
    client.cookies.set("session_token", token)

    res = client.post("/api/auth/logout")
    assert res.status_code == 200
    assert res.json()["status"] == "logged_out"


def test_robinhood_sso_redirect_flow(client):
    res = client.get("/api/auth/robinhood", follow_redirects=False)
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("https://robinhood.com/mcp/trading?")
    assert "code_challenge_method=S256" in location
    assert "code_challenge=" in location
    assert "client_id=" in location
    assert "state=" in location

    # Check that cookies for verifier, state, and redirect_uri were set
    cookies = res.cookies
    assert "oauth_verifier" in cookies
    assert "oauth_state" in cookies
    assert "oauth_redirect_uri" in cookies

