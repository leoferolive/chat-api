"""FastAPI app entrypoint: routes, CORS, lifespan."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .config import Settings, get_settings
from .db import Database, hash_ip
from .guards import (
    SESSION_COOKIE,
    CostGateExceeded,
    build_limiter,
    client_ip,
    cost_gate_check,
    issue_session_token,
    require_first_message_or_session,
    verify_turnstile,
)
from .llm_router import AllProvidersFailed, stream_completion
from .metrics import (
    CHAT_DURATION_SECONDS,
    CHATS_TOTAL,
    COST_GATE_HITS_TOTAL,
    DAILY_CALLS,
    RATE_LIMIT_HITS_TOTAL,
    UNKNOWN_MODEL,
    set_info,
)
from .models import ChatRequest
from .prompt import build_messages, refusal_text
from .router import pick_paths
from .sse import build_response, sse_payload
from .user_identity import cap_user_label, normalize_user_label, sanitize_user_name
from .wiki_loader import WikiLoader


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger("chat-api")


def _is_internal_host(host_header: str) -> bool:
    """True if Host is an IP literal or localhost — i.e. an in-cluster scrape."""
    host = host_header.split(":", 1)[0].strip().lower()
    if host in {"localhost", ""}:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging()

    loader = WikiLoader(settings.wiki_dir, poll_seconds=settings.wiki_poll_seconds)
    db = Database(settings.db_path)
    await db.connect()

    app.state.settings = settings
    app.state.wiki_loader = loader
    app.state.db = db

    DAILY_CALLS.set(await db.count_calls_today())
    set_info(version=app.version, env=settings.env)

    log.info("startup", env=settings.env, wiki_dir=str(settings.wiki_dir))
    try:
        yield
    finally:
        await db.close()
        log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="chat-api", version="0.1.0", lifespan=lifespan)

    limiter = build_limiter(settings.rate_limit_per_ip)
    app.state.limiter = limiter

    def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
        # Only the /chat/stream route has @limiter.limit applied, so this
        # handler also fires only for that path. We track the hit on the
        # dedicated counter; chats_total stays a clean count of streams.
        RATE_LIMIT_HITS_TOTAL.inc()
        return _rate_limit_exceeded_handler(request, exc)

    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        # The Service is also reachable via Ingress on the public host. Refuse
        # /metrics for any Host that doesn't look like a Pod IP — Prometheus
        # scrapes via Pod IP (10.42.x.x), public clients always see a domain.
        if not _is_internal_host(request.headers.get("host", "")):
            return Response(status_code=404)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/chat/stream")
    @limiter.limit(settings.rate_limit_per_ip)
    async def chat_stream(
        request: Request,
        body: ChatRequest,
        settings: Settings = Depends(get_settings),  # noqa: B008 — FastAPI dependency pattern
    ) -> Response:
        return await _handle_chat_stream(request, body, settings)

    return app


async def _handle_chat_stream(
    request: Request,
    body: ChatRequest,
    settings: Settings,
) -> Response:
    started = time.monotonic()
    db: Database = request.app.state.db
    loader: WikiLoader = request.app.state.wiki_loader

    ip = client_ip(request)
    is_first_message = len(body.messages) == 1 and body.messages[0].role == "user"
    user_raw = sanitize_user_name(body.userName)
    user_label = cap_user_label(
        normalize_user_label(body.userName, salt=settings.user_hash_salt)
    )

    # Turnstile / session enforcement BEFORE we touch the LLM.
    turnstile_ok = await verify_turnstile(body.turnstileToken, settings, remote_ip=ip)
    require_first_message_or_session(
        request,
        is_first_message=is_first_message,
        session_id=body.sessionId,
        turnstile_ok=turnstile_ok,
        settings=settings,
    )

    # Cost gate
    try:
        await cost_gate_check(db, settings.daily_llm_call_limit)
    except CostGateExceeded as exc:
        gate_msg = str(exc)
        # Don't observe chat_duration here — the request never streamed,
        # near-zero observations would skew the latency p95/p99 panels.
        COST_GATE_HITS_TOTAL.inc()
        CHATS_TOTAL.labels(
            status="cost_gate",
            model=UNKNOWN_MODEL,
            lang=body.lang,
            user=user_label,
        ).inc()

        async def gate_gen():
            yield {"data": sse_payload({"type": "error", "message": gate_msg})}

        resp = build_response(gate_gen())
        resp.status_code = 503
        return resp

    # Per-session soft cap (drawer rotates sessionId on reload).
    session_count = await db.count_user_messages_in_session(body.sessionId)
    if session_count >= settings.messages_per_session_limit:
        log.warning(
            "session_limit_reached",
            session_id=body.sessionId,
            count=session_count,
            limit=settings.messages_per_session_limit,
        )

        async def session_limit_gen():
            yield {"data": sse_payload({"type": "error", "message": "session_limit_reached"})}

        resp = build_response(session_limit_gen())
        resp.status_code = 429
        return resp

    # Per-IP daily ceiling (real defence against rotating sessionIds).
    ip_hashed_pre = hash_ip(ip, settings.ip_hash_salt)
    ip_calls_today = await db.count_calls_today_by_ip(ip_hashed_pre)
    if ip_calls_today >= settings.daily_calls_per_ip_limit:
        log.warning(
            "ip_daily_limit_reached",
            ip_hash_prefix=ip_hashed_pre[:8],
            count=ip_calls_today,
            limit=settings.daily_calls_per_ip_limit,
        )

        async def ip_limit_gen():
            yield {"data": sse_payload({"type": "error", "message": "ip_daily_limit"})}

        resp = build_response(ip_limit_gen())
        resp.status_code = 429
        return resp

    # Persist session row + user turn (fire-and-forget).
    ip_hashed = ip_hashed_pre
    asyncio.create_task(
        db.upsert_session(body.sessionId, ip_hashed, body.lang, user_name=user_raw)
    )
    user_msg = body.messages[-1]
    if user_msg.role == "user":
        asyncio.create_task(
            db.save_turn(
                session_id=body.sessionId,
                role="user",
                content=user_msg.content,
            )
        )

    # Bump the daily call counter once per turn, BEFORE the router runs.
    # The router itself spends a provider call even when the answer LLM is
    # short-circuited (refusal path). If we incremented only on the answer
    # path, off-topic floods would never trip the daily cost gate.
    await db.increment_calls_today()

    # LLM router: ask the model itself which wiki pages to ground on.
    selected_paths = await pick_paths(
        question=user_msg.content,
        history=body.messages,
        lang=body.lang,
        loader=loader,
        providers=settings.provider_list,
        settings=settings,
    )

    if not selected_paths:
        # Out of scope (or router failed): refuse without invoking the answer
        # LLM. The router call itself was already counted above.
        refusal = refusal_text(body.lang)
        # Persist the assistant turn so the UI shows it on reload.
        asyncio.create_task(
            db.save_turn(
                session_id=body.sessionId,
                role="assistant",
                content=refusal,
            )
        )
        CHATS_TOTAL.labels(
            status="refused",
            model=UNKNOWN_MODEL,
            lang=body.lang,
            user=user_label,
        ).inc()
        log.info(
            "router_refused",
            session_id=body.sessionId,
            lang=body.lang,
            user=user_label,
        )

        async def refusal_gen():
            yield {"data": sse_payload({"type": "token", "value": refusal})}
            yield {
                "data": sse_payload(
                    {"type": "done", "model": UNKNOWN_MODEL, "tokens": {"prompt": 0, "completion": 0}}
                )
            }

        response = build_response(refusal_gen())
        if is_first_message:
            token = issue_session_token(body.sessionId, settings)
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                secure=settings.env == "prod",
                max_age=settings.session_ttl_seconds,
            )
        return response

    pages = [p for p in (loader.get_page(path) for path in selected_paths) if p is not None]
    messages_for_llm = build_messages(body.lang, pages, body.messages)

    async def event_gen() -> AsyncIterator[dict]:
        model_used = ""
        full_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        provider_attempts: list[str] = []
        try:
            async for ev in stream_completion(messages_for_llm, settings.provider_list):
                if ev["type"] == "start":
                    model_used = ev["model"]
                    continue
                if ev["type"] == "token":
                    yield {"data": sse_payload({"type": "token", "value": ev["value"]})}
                elif ev["type"] == "done":
                    model_used = ev["model"]
                    full_text = ev.get("text", "")
                    prompt_tokens = ev["tokens"].get("prompt", 0)
                    completion_tokens = ev["tokens"].get("completion", 0)
                    provider_attempts = ev.get("attempts", [])
                    yield {
                        "data": sse_payload(
                            {
                                "type": "done",
                                "model": model_used,
                                "tokens": ev["tokens"],
                            }
                        )
                    }
        except AllProvidersFailed as exc:
            log.error("all_providers_failed", err=str(exc), session=body.sessionId)
            CHATS_TOTAL.labels(
                status="error",
                model=model_used or UNKNOWN_MODEL,
                lang=body.lang,
                user=user_label,
            ).inc()
            CHAT_DURATION_SECONDS.labels(
                model=model_used or UNKNOWN_MODEL, status="error"
            ).observe(time.monotonic() - started)
            yield {"data": sse_payload({"type": "error", "message": "all_providers_failed"})}
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("stream_failed", err=str(exc), session=body.sessionId)
            CHATS_TOTAL.labels(
                status="error",
                model=model_used or UNKNOWN_MODEL,
                lang=body.lang,
                user=user_label,
            ).inc()
            CHAT_DURATION_SECONDS.labels(
                model=model_used or UNKNOWN_MODEL, status="error"
            ).observe(time.monotonic() - started)
            yield {"data": sse_payload({"type": "error", "message": "stream_failed"})}
            return

        elapsed = time.monotonic() - started
        latency_ms = int(elapsed * 1000)
        CHATS_TOTAL.labels(
            status="ok",
            model=model_used or UNKNOWN_MODEL,
            lang=body.lang,
            user=user_label,
        ).inc()
        CHAT_DURATION_SECONDS.labels(
            model=model_used or UNKNOWN_MODEL, status="ok"
        ).observe(elapsed)
        log.info(
            "chat_completed",
            session_id=body.sessionId,
            lang=body.lang,
            user=user_label,
            model_used=model_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            provider_attempts=provider_attempts,
            wiki_pages=[p.path for p in pages],
        )

        if full_text:
            asyncio.create_task(
                db.save_turn(
                    session_id=body.sessionId,
                    role="assistant",
                    content=full_text,
                    model=model_used,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                )
            )

    response = build_response(event_gen())

    # Issue / refresh the session cookie so subsequent messages skip Turnstile.
    if is_first_message:
        token = issue_session_token(body.sessionId, settings)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=settings.env == "prod",
            max_age=settings.session_ttl_seconds,
        )
    return response


# Default app for `uvicorn app.main:app` invocations.
app = create_app()
