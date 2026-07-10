"""FastAPI app entrypoint: routes, CORS, lifespan."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .config import Settings, get_settings
from .cost import provider_of
from .db import Database, hash_ip
from .guards import (
    CostGateExceeded,
    build_limiter,
    client_ip,
    cost_gate_check,
    require_first_message_or_session,
    set_session_cookie,
    verify_turnstile,
)
from .llm_router import AllProvidersFailed, stream_completion
from .metrics import (
    CHAT_DURATION_SECONDS,
    CHATS_TOTAL,
    COST_GATE_HITS_TOTAL,
    DAILY_CALLS,
    RATE_LIMIT_HITS_TOTAL,
    SESSIONS_CREATED_TOTAL,
    UNKNOWN_MODEL,
    set_info,
)
from .models import ChatRequest
from .prompt import build_messages, refusal_text
from .router import decision_hash, pick_paths
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

# Keeps a strong reference to every fire-and-forget task until it finishes —
# asyncio only weakly tracks tasks created via create_task(), so without this
# the task can be garbage-collected mid-flight. See `_fire_and_forget`.
_background_tasks: set[asyncio.Task[None]] = set()


def _fire_and_forget(coro: Coroutine[Any, Any, None], **log_ctx: Any) -> asyncio.Task[None]:
    """Schedule ``coro`` without awaiting it, but never let its failure vanish.

    A bare ``asyncio.create_task(...)`` has two footguns: (1) the task can be
    garbage-collected before it runs if nothing holds a reference, and (2) if
    it raises, the exception is only ever reported as a swallowed "Task
    exception was never retrieved" warning — invisible in our structlog JSON
    output. This keeps the task alive in a module-level set and logs any
    exception (with caller-supplied context, e.g. session_id / turn role)
    before dropping the reference.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(done_task: asyncio.Task[None]) -> None:
        _background_tasks.discard(done_task)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            log.error(
                "background_task_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                **log_ctx,
            )

    task.add_done_callback(_on_done)
    return task


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
        # Drain any in-flight fire-and-forget tasks (e.g. save_turn /
        # session upsert) before closing the DB connection they depend on —
        # otherwise a task still in flight during shutdown can hit an
        # already-closed connection. Exceptions are already logged by
        # `_fire_and_forget`'s own done callback, so just wait them out.
        if _background_tasks:
            await asyncio.gather(*_background_tasks, return_exceptions=True)
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

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        # Honour an upstream X-Request-Id (set by Traefik / a tracing proxy)
        # so a request crossing services can be correlated by a single id.
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-Id"] = rid
        return response

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

    @app.get("/metrics-traffic")
    async def metrics_traffic(request: Request) -> Response:
        # Unique-visitor gauges built per-scrape from the SQLite. Using
        # gauges (not counters) because "unique X in the last 24h" goes
        # both up and down as the rolling window advances. Prometheus
        # counters can only ever grow.
        if not _is_internal_host(request.headers.get("host", "")):
            return Response(status_code=404)
        db: Database = request.app.state.db
        now = int(time.time())
        windows = {
            "today": now - 24 * 3600,
            "7d": now - 7 * 24 * 3600,
            "30d": now - 30 * 24 * 3600,
        }
        reg = CollectorRegistry()
        unique_sessions = Gauge(
            "chat_api_unique_sessions",
            "Distinct sessionIds with at least one user message in the window.",
            labelnames=("window",),
            registry=reg,
        )
        unique_ips = Gauge(
            "chat_api_unique_ips",
            "Distinct (salted) IP hashes that opened a session in the window.",
            labelnames=("window",),
            registry=reg,
        )
        sessions_by_lang = Gauge(
            "chat_api_sessions_by_lang",
            "Sessions created in the 24h window, grouped by lang.",
            labelnames=("lang",),
            registry=reg,
        )
        for window_name, since_ts in windows.items():
            unique_sessions.labels(window=window_name).set(
                await db.distinct_sessions_since(since_ts)
            )
            unique_ips.labels(window=window_name).set(await db.distinct_ips_since(since_ts))
        for row in await db.sessions_by_lang_since(windows["today"]):
            sessions_by_lang.labels(lang=row["lang"]).set(row["count"])
        return Response(content=generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    @app.get("/metrics-judge")
    async def metrics_judge(request: Request) -> Response:
        # Same internal-only guard as /metrics. The judge runs in a separate
        # CronJob process, so its counters live in the shared SQLite — we
        # build a fresh CollectorRegistry per scrape from current aggregates.
        if not _is_internal_host(request.headers.get("host", "")):
            return Response(status_code=404)
        db: Database = request.app.state.db
        # 24h window — long enough to survive a Prometheus restart, short
        # enough that "average score yesterday" stays informative.
        since_ts = int(time.time()) - 24 * 3600
        aggregates = await db.judge_score_aggregates(since_ts)
        verdicts = await db.judge_verdict_counts(since_ts)

        reg = CollectorRegistry()
        score_avg = Gauge(
            "chat_api_judge_score_avg",
            "Average judge score (24h window) per criterion / answer model / judge model.",
            labelnames=("criterion", "answer_model", "judge_model"),
            registry=reg,
        )
        score_count = Gauge(
            "chat_api_judge_evaluations_total",
            "Number of judge evaluations recorded in the 24h window.",
            labelnames=("criterion", "answer_model", "judge_model"),
            registry=reg,
        )
        verdict_count = Gauge(
            "chat_api_judge_verdicts_total",
            "Judge verdict bucket counts (pass>=4, warn=3, fail<3) in the 24h window.",
            labelnames=("criterion", "verdict"),
            registry=reg,
        )
        for row in aggregates:
            score_avg.labels(
                criterion=row["criterion"],
                answer_model=row["answer_model"],
                judge_model=row["judge_model"],
            ).set(row["avg_score"])
            score_count.labels(
                criterion=row["criterion"],
                answer_model=row["answer_model"],
                judge_model=row["judge_model"],
            ).set(row["count"])
        for row in verdicts:
            verdict_count.labels(criterion=row["criterion"], verdict=row["verdict"]).set(
                row["count"]
            )
        return Response(content=generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

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
    user_label = cap_user_label(normalize_user_label(body.userName, salt=settings.user_hash_salt))
    user_msg = body.messages[-1]

    # Turnstile / session enforcement BEFORE we touch the LLM.
    turnstile_ok = await verify_turnstile(body.turnstileToken, settings, remote_ip=ip)
    require_first_message_or_session(
        request,
        is_first_message=is_first_message,
        session_id=body.sessionId,
        turnstile_ok=turnstile_ok,
        settings=settings,
    )

    # Cost gate: the check *is* the atomic increment (see
    # guards.cost_gate_check / Database.increment_calls_today) — this closes
    # the TOCTOU where a plain SELECT here and the real increment, several
    # awaits and gates later, let concurrent requests all pass before any of
    # them counted. If a later gate (session/IP) rejects this same request we
    # decrement below, so the daily counter still only reflects requests
    # that reached the LLM router — unchanged from before.
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

    # Per-session soft cap (drawer rotates sessionId on reload). The
    # count-then-insert is folded into one atomic statement
    # (`try_reserve_session_message`) so concurrent requests for the same
    # sessionId can't all observe "under the cap" and all get through — the
    # same TOCTOU as the cost gate, just against a per-session count instead
    # of the global one. This also persists the user turn immediately,
    # replacing the old fire-and-forget save for it.
    reserved_message_id: int | None = None
    if user_msg.role == "user":
        reserved_message_id = await db.try_reserve_session_message(
            body.sessionId, user_msg.content, settings.messages_per_session_limit
        )
        session_gate_ok = reserved_message_id is not None
    else:
        # Defensive branch: the last message in the payload isn't a user
        # turn, so there is nothing to reserve/persist here — fall back to
        # the plain read check, exactly like before this fix.
        session_gate_ok = (
            await db.count_user_messages_in_session(body.sessionId)
        ) < settings.messages_per_session_limit

    if not session_gate_ok:
        await db.decrement_calls_today()
        session_count = await db.count_user_messages_in_session(body.sessionId)
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

    # Per-IP daily ceiling (real defence against rotating sessionIds). This
    # counts *served* assistant turns (see count_calls_today_by_ip), which
    # only exist once a response actually gets persisted — reserving it
    # atomically like the two gates above would mean rolling the
    # reservation back across every exit path of the streaming response
    # (success, provider failure, client disconnect, cancellation) just to
    # keep excluding calls that never got served, which this SELECT-based
    # check already does correctly. Left as a plain read; the window it
    # still has is markedly narrower now that the two gates above close
    # first and stop the bulk of a concurrent burst.
    ip_hashed_pre = hash_ip(ip, settings.ip_hash_salt)
    ip_calls_today = await db.count_calls_today_by_ip(ip_hashed_pre)
    if ip_calls_today >= settings.daily_calls_per_ip_limit:
        if reserved_message_id is not None:
            await db.delete_message(reserved_message_id)
        await db.decrement_calls_today()
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

    # Persist the session row (fire-and-forget). The user turn itself was
    # already persisted above by try_reserve_session_message.
    ip_hashed = ip_hashed_pre

    async def _upsert_and_count() -> None:
        created = await db.upsert_session(body.sessionId, ip_hashed, body.lang, user_name=user_raw)
        if created:
            SESSIONS_CREATED_TOTAL.labels(lang=body.lang).inc()

    _fire_and_forget(_upsert_and_count(), session_id=body.sessionId, turn="session_upsert")

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
        _fire_and_forget(
            db.save_turn(
                session_id=body.sessionId,
                role="assistant",
                content=refusal,
            ),
            session_id=body.sessionId,
            turn="assistant_refusal",
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
                    {
                        "type": "done",
                        "model": UNKNOWN_MODEL,
                        "tokens": {"prompt": 0, "completion": 0},
                    }
                )
            }

        response = build_response(refusal_gen())
        if is_first_message:
            set_session_cookie(response, body.sessionId, settings)
        return response

    pages = [p for p in (loader.get_page(path) for path in selected_paths) if p is not None]
    messages_for_llm = build_messages(body.lang, pages, body.messages)
    paths_hash = decision_hash(selected_paths)

    async def event_gen() -> AsyncIterator[dict]:
        model_used = ""
        full_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        cost_usd = 0.0
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
                    cost_usd = ev.get("cost_usd", 0.0)
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
            CHAT_DURATION_SECONDS.labels(model=model_used or UNKNOWN_MODEL, status="error").observe(
                time.monotonic() - started
            )
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
            CHAT_DURATION_SECONDS.labels(model=model_used or UNKNOWN_MODEL, status="error").observe(
                time.monotonic() - started
            )
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
        CHAT_DURATION_SECONDS.labels(model=model_used or UNKNOWN_MODEL, status="ok").observe(
            elapsed
        )
        log.info(
            "chat_completed",
            stage="answer",
            session_id=body.sessionId,
            lang=body.lang,
            user=user_label,
            model_used=model_used,
            provider=provider_of(model_used),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            decision_paths=paths_hash,
            provider_attempts=provider_attempts,
            wiki_pages=[p.path for p in pages],
        )

        if full_text:
            _fire_and_forget(
                db.save_turn(
                    session_id=body.sessionId,
                    role="assistant",
                    content=full_text,
                    model=model_used,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                ),
                session_id=body.sessionId,
                turn="assistant",
            )

    response = build_response(event_gen())

    # Issue / refresh the session cookie so subsequent messages skip Turnstile.
    if is_first_message:
        set_session_cookie(response, body.sessionId, settings)
    return response


# Default app for `uvicorn app.main:app` invocations.
app = create_app()
