"""Auth: open demo by default; Google Sign-In when configured / required."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

ALGORITHM = "HS256"
TOKEN_HOURS = 24
_bearer = HTTPBearer(auto_error=False)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def is_demo() -> bool:
    flag = _env("DEMO_MODE", "true").lower()
    return flag not in {"0", "false", "no", "private", "off"}


def google_client_id() -> str:
    return _env("GOOGLE_CLIENT_ID")


def _secret() -> str:
    secret = _env("AUTH_SECRET")
    if secret:
        return secret
    client_id = google_client_id()
    if client_id:
        return f"docmind-dev-{client_id}"
    if is_demo():
        return "demo-unused"
    raise RuntimeError("Set AUTH_SECRET (and GOOGLE_CLIENT_ID) for private mode.")


def issue_token(subject: str) -> str:
    payload = {
        "sub": subject,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def login_with_google(credential: str) -> tuple[str, str]:
    client_id = google_client_id()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")
    try:
        info = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Google could not verify this sign-in.")
    if info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=401, detail="Invalid Google token.")
    email = (info.get("email") or "").strip()
    if not email or not info.get("email_verified"):
        raise HTTPException(status_code=401, detail="Google did not return a verified email.")
    return issue_token(email), email


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    if creds is None or creds.scheme.lower() != "bearer":
        if is_demo():
            return "demo"
        raise HTTPException(status_code=401, detail="Sign in with Google to continue.")
    try:
        payload = jwt.decode(creds.credentials, _secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        if is_demo():
            return "demo"
        raise HTTPException(status_code=401, detail="Session expired. Sign in again.")
    user = payload.get("sub")
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return str(user)
