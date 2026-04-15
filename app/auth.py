from __future__ import annotations

import hmac
import os
import re

from fastapi import Depends, HTTPException, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette import status


_bearer = HTTPBearer(auto_error=False)


def auth_enabled() -> bool:
    return bool(_expected_token())


def _expected_token() -> str:
    return _normalize_token(os.getenv("SDR_GATEWAY_API_TOKEN", ""))


def _normalize_token(raw: str | None) -> str:
    token = (raw or "").strip().strip('"').strip("'")

    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()

    # Accept accidental "SDR_GATEWAY_API_TOKEN=<value>" formatting.
    if token.upper().startswith("SDR_GATEWAY_API_TOKEN="):
        token = token.split("=", 1)[1].strip().strip('"').strip("'")

    # Remove accidental internal whitespace/newlines.
    token = "".join(token.split())

    # If token includes surrounding noise, extract a base64-ish segment.
    if "=" in token and len(token) < 16:
        return token
    match = re.search(r"([A-Za-z0-9+/=_-]{16,})", token)
    if match:
        token = match.group(1)

    return token


def _extract_http_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials.strip()
    x_api_key = request.headers.get("x-api-key", "").strip()
    if x_api_key:
        return x_api_key
    return None


def _token_valid(candidate: str | None) -> bool:
    expected = _expected_token()
    if not expected:
        return True
    candidate_norm = _normalize_token(candidate)
    if not candidate_norm:
        return False
    return hmac.compare_digest(candidate_norm, expected)


def require_http_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    candidate = _extract_http_token(request, credentials)
    if not _token_valid(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_ws_auth(websocket: WebSocket) -> bool:
    expected = _expected_token()
    if not expected:
        return True

    candidate = websocket.query_params.get("token")
    if not candidate:
        auth_header = websocket.headers.get("authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            candidate = auth_header[7:].strip()
    if not candidate:
        candidate = websocket.headers.get("x-api-key", "").strip()

    if not _token_valid(candidate):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
        return False
    return True
