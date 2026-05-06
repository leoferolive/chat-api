"""FastAPI app entrypoint: routes, CORS, lifespan."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
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
from .models import ChatRequest
from .prompt import build_messages
from .retriever import Retriever
from .sse import build_response, sse_payload
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging()

    loader = WikiLoader(settings.wiki_dir, poll_seconds=settings.wiki_poll_seconds)
    retriever = Retriever(loader)
    db = Database(settings.db_path)
    await db.connect()

    app.state.settings = settings
    app.state.wiki_loader = loader
    app.state.retriever = retriever
    app.state.db = db

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
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
    retriever: Retriever = request.app.state.retriever

    ip = client_ip(request)
    is_first_message = len(body.messages) == 1 and body.messages[0].role == "user"

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

        async def gate_gen():
            yield {"data": sse_payload({"type": "error", "message": gate_msg})}

        resp = build_response(gate_gen())
        resp.status_code = 503
        return resp

    # Persist session row + user turn (fire-and-forget).
    ip_hashed = hash_ip(ip, settings.ip_hash_salt)
    asyncio.create_task(db.upsert_session(body.sessionId, ip_hashed, body.lang))
    user_msg = body.messages[-1]
    if user_msg.role == "user":
        asyncio.create_task(
            db.save_turn(
                session_id=body.sessionId,
                role="user",
                content=user_msg.content,
            )
        )

    # Retrieve wiki context.
    pages = retriever.pick(user_msg.content, body.lang, top_n=settings.retriever_top_n)
    messages_for_llm = build_messages(body.lang, pages, body.messages)

    # Bump call counter just before invoking the LLM.
    await db.increment_calls_today()

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
            yield {"data": sse_payload({"type": "error", "message": "all_providers_failed"})}
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("stream_failed", err=str(exc), session=body.sessionId)
            yield {"data": sse_payload({"type": "error", "message": "stream_failed"})}
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "chat_completed",
            session_id=body.sessionId,
            lang=body.lang,
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
