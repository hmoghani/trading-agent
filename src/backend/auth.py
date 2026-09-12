"""Authentication, Robinhood OAuth SSO, and session management."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from fastapi import HTTPException, Request, status
import jwt
import requests

from src.backend.config import settings

logger = logging.getLogger(__name__)

ROBINHOOD_AUTH_ENDPOINT = "https://robinhood.com/mcp/trading"
ROBINHOOD_REGISTRATION_ENDPOINT = "https://agent.robinhood.com/oauth/trading/register"
ROBINHOOD_TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
ROBINHOOD_RESOURCE = "https://agent.robinhood.com/mcp/trading"

# Cache dynamically registered client IDs per redirect URI
_CLIENT_REGISTRY: Dict[str, str] = {}


def verify_dashboard_password(password: str) -> bool:
    """Verify master dashboard password with constant-time comparison."""
    if not settings.dashboard_password:
        return True
    return secrets.compare_digest(password.strip(), settings.dashboard_password.strip())


def generate_code_verifier() -> str:
    """Generate cryptographically random PKCE code verifier."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")


def generate_code_challenge(verifier: str) -> str:
    """Compute S256 code challenge from the code verifier."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


import glob

def get_cached_robinhood_token() -> Optional[Dict[str, Any]]:
    """Check ~/.mcp-auth for an existing valid Robinhood access token."""
    try:
        token_files = glob.glob(os.path.expanduser("~/.mcp-auth/**/aa46f9788cb2ecfca540f6bfe1576d4e_tokens.json"), recursive=True)
        if not token_files:
            token_files = glob.glob(os.path.expanduser("~/.mcp-auth/**/*token*.json"), recursive=True)
        for tf in token_files:
            with open(tf, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("access_token"):
                    return data
    except Exception as e:
        logger.debug(f"Could not read cached token: {e}")
    return None


def get_or_register_client_id(redirect_uri: str) -> str:
    """Register or retrieve cached OAuth client ID with Robinhood."""
    if redirect_uri in _CLIENT_REGISTRY:
        return _CLIENT_REGISTRY[redirect_uri]

    try:
        payload = {
            "client_name": "Robinhood Trading",
            "redirect_uris": list(set([
                "http://localhost:8000/api/auth/callback",
                "http://127.0.0.1:8000/api/auth/callback",
                redirect_uri,
            ])),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "internal",
        }
        res = requests.post(ROBINHOOD_REGISTRATION_ENDPOINT, json=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        client_id = data["client_id"]
        _CLIENT_REGISTRY[redirect_uri] = client_id
        return client_id
    except Exception as e:
        logger.warning(f"Dynamic client registration fallback: {e}")
        # Default fallback client ID
        fallback_id = "LtLiNmbs9owbYfWgBlC68Z2VujIPuvGoAiSYr8xW"
        _CLIENT_REGISTRY[redirect_uri] = fallback_id
        return fallback_id


def build_robinhood_sso_url(redirect_uri: str, code_challenge: str, state: str) -> str:
    """Build the official Robinhood SSO authorization URL."""
    client_id = get_or_register_client_id(redirect_uri)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "internal",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": ROBINHOOD_RESOURCE,
        "state": state,
    }
    return f"{ROBINHOOD_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_token(
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> Dict[str, Any]:
    """Exchange authorization code and PKCE verifier for Robinhood access token."""
    client_id = get_or_register_client_id(redirect_uri)
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "resource": ROBINHOOD_RESOURCE,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    res = requests.post(ROBINHOOD_TOKEN_URL, data=data, headers=headers, timeout=15)
    res.raise_for_status()
    token_data = res.json()

    # Cache token in ~/.mcp-auth so mcp-remote bridge can use it
    try:
        home = os.path.expanduser("~")
        auth_dir = os.path.join(home, ".mcp-auth")
        os.makedirs(auth_dir, exist_ok=True)
        token_path = os.path.join(auth_dir, "token.json")
        with open(token_path, "w") as f:
            json.dump(token_data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to cache token to ~/.mcp-auth: {e}")

    return token_data


def create_session_jwt(user_data: Dict[str, Any]) -> str:
    """Create a signed JWT session cookie."""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.session_expire_hours)
    payload = {
        "sub": user_data.get("username", "robinhood_user"),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "data": user_data,
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


def decode_session_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a session JWT."""
    try:
        decoded = jwt.decode(token, settings.session_secret, algorithms=["HS256"])
        return decoded.get("data")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency to authenticate requests via session cookie or Bearer header."""
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in with Robinhood.",
        )

    user_data = decode_session_jwt(token)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please sign in again.",
        )

    return user_data


def get_optional_user(request: Request) -> Optional[Dict[str, Any]]:
    """Return user data if session is valid, otherwise None without raising."""
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
    if not token:
        return None
    return decode_session_jwt(token)
