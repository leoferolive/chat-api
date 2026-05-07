"""Request-time defences: Turnstile, rate limit, daily cost gate, session JWT."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from fastapi import HTTPException, Request
from jose import JWTError, jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import Settings

logger = structlog.get_logger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


# --- IP extraction --------------------------------------------------------


def client_ip(request: Request) -> str:
    """Resolve the real client IP (Cloudflare-aware)."""
    headers = request.headers
    cf_ip = headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    fwd = headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return get_remote_address(request)


def _limiter_key(request: Request) -> str:
    return client_ip(request)


def build_limiter(default_rate: str) -> Limiter:
    return Limiter(key_func=_limiter_key, default_limits=[default_rate])


# --- Turnstile ------------------------------------------------------------


async def verify_turnstile(token: str | None, settings: Settings, *, remote_ip: str | None = None) -> bool:
    """Validate a Turnstile token against Cloudflare. Returns True on success."""
    if settings.turnstile_disabled:
        return True
    if not token:
        logger.warning("turnstile_token_missing", remote_ip=remote_ip)
        return False

    payload: dict[str, Any] = {
        "secret": settings.turnstile_secret,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(TURNSTILE_VERIFY_URL, data=payload)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("turnstile_transport_error", err=str(exc))
        return False

    success = bool(body.get("success"))
    if not success:
        logger.warning(
            "turnstile_rejected",
            error_codes=body.get("error-codes", []),
            hostname=body.get("hostname"),
            challenge_ts=body.get("challenge_ts"),
            remote_ip=remote_ip,
            token_prefix=token[:20] if token else None,
        )
    return success


# --- Session JWT ----------------------------------------------------------


SESSION_COOKIE = "chat_session"


def issue_session_token(session_id: str, settings: Settings) -> str:
    now = int(time.time())
    payload = {
        "sid": session_id,
        "iat": now,
        "exp": now + settings.session_ttl_seconds,
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


def verify_session_token(token: str, settings: Settings) -> str | None:
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=["HS256"])
    except JWTError:
        return None
    return payload.get("sid")


def require_first_message_or_session(
    request: Request,
    *,
    is_first_message: bool,
    session_id: str,
    turnstile_ok: bool,
    settings: Settings,
) -> None:
    """Enforce Turnstile on the first message; cookie-session afterwards."""
    if is_first_message:
        if not turnstile_ok:
            raise HTTPException(status_code=403, detail="turnstile_failed")
        return

    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise HTTPException(status_code=403, detail="missing_session_cookie")
    sid = verify_session_token(cookie, settings)
    if sid != session_id:
        raise HTTPException(status_code=403, detail="invalid_session_cookie")


# --- Cost gate ------------------------------------------------------------


class CostGateExceeded(RuntimeError):
    """Raised when the daily LLM call ceiling has been hit."""


async def cost_gate_check(db: Any, limit: int) -> int:
    """Return current count if under limit, else raise CostGateExceeded."""
    count = await db.count_calls_today()
    if count >= limit:
        raise CostGateExceeded(f"daily LLM call limit reached: {count}/{limit}")
    return count
