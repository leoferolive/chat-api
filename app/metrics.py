"""Prometheus metrics for chat-api.

All metrics live on the default registry; `prometheus_client.make_asgi_app()`
mounted at /metrics exposes them. Registered at import time on a single
shared registry — labels are applied on each `.inc()`/`.observe()` call.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

CHATS_TOTAL = Counter(
    "chat_api_chats_total",
    "Total chat completions handled, by outcome, model, language and user.",
    labelnames=("status", "model", "lang", "user"),
)

CHAT_DURATION_SECONDS = Histogram(
    "chat_api_chat_duration_seconds",
    "Wall-clock duration of /chat/stream requests, by model and status.",
    labelnames=("model", "status"),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
)

TOKENS_TOTAL = Counter(
    "chat_api_tokens_total",
    "LLM tokens consumed, by kind (prompt|completion) and model.",
    labelnames=("kind", "model"),
)

COST_USD_TOTAL = Counter(
    "chat_api_cost_usd_total",
    "Estimated USD cost of LLM calls, by model, provider and stage (router|answer).",
    labelnames=("model", "provider", "stage"),
)

PROVIDER_FAILURES_TOTAL = Counter(
    "chat_api_provider_failures_total",
    "LLM provider failures, by model and phase (open|early|mid).",
    labelnames=("model", "phase"),
)

PROVIDER_ATTEMPTS_TOTAL = Counter(
    "chat_api_provider_attempts_total",
    "LLM provider attempts, by model and result (success|failure).",
    labelnames=("model", "result"),
)

COST_GATE_HITS_TOTAL = Counter(
    "chat_api_cost_gate_hits_total",
    "Times the daily LLM call cost gate blocked a request.",
)

RATE_LIMIT_HITS_TOTAL = Counter(
    "chat_api_rate_limit_hits_total",
    "Times the per-IP rate limit blocked a request.",
)

DAILY_CALLS = Gauge(
    "chat_api_daily_calls",
    "Current count of LLM calls made today (UTC).",
)

ROUTER_OUTCOME_TOTAL = Counter(
    "chat_api_router_outcome_total",
    "Outcomes of the LLM router that picks wiki pages per question.",
    labelnames=("outcome",),  # ok | empty | parse_error | provider_error
)

ROUTER_SELECTED_PAGES = Histogram(
    "chat_api_router_selected_pages",
    "Number of wiki pages chosen by the LLM router per request.",
    buckets=(0, 1, 2, 3, 4, 5),
)

INFO = Info(
    "chat_api",
    "Build/runtime info for chat-api.",
)


def set_info(*, version: str, env: str) -> None:
    INFO.info({"version": version, "env": env})


UNKNOWN_MODEL = "unknown"
