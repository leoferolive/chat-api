# chat-api

Chat backend for `leoferolive.com.br`. Receives questions from the site,
selects relevant pages from a markdown LLM-Wiki (Karpathy pattern), builds
a system prompt and streams a response from a remote LLM via LiteLLM
(multi-provider with fallback).

Stack: Python 3.12 · FastAPI · LiteLLM · aiosqlite · slowapi · sse-starlette · structlog.

## Layout

```
app/
  main.py          FastAPI app, /chat/stream, /healthz, lifespan
  config.py        pydantic-settings (env)
  models.py        ChatRequest / ChatChunk / WikiPage
  wiki_loader.py   reads WIKI_DIR, parses index.md, polling cache
  retriever.py     keyword-overlap scorer (v1)
  prompt.py        system persona PT/EN + wiki context block
  llm_router.py    litellm.acompletion(stream=True) + fallback
  guards.py        Turnstile, slowapi, cost gate, session JWT
  db.py            aiosqlite sessions / messages / daily_calls
  sse.py           EventSourceResponse helper
tests/             pytest + httpx + asgi lifespan, LiteLLM mocked
wiki-fixture/      tiny wiki used in dev / tests
```

## Run locally

```bash
# 1. install deps
uv sync --all-extras

# 2. bootstrap env
cp .env.example .env
#   leave TURNSTILE_DISABLED=true while developing
#   set GEMINI_API_KEY / OPENROUTER_API_KEY for real model calls

# 3. start
uv run uvicorn app.main:app --reload
```

Smoke test against the SSE endpoint:

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{
        "sessionId":"local-1",
        "messages":[{"role":"user","content":"Como foi o trabalho do Leonardo na Wiley?"}],
        "lang":"pt"
      }'
```

Expect `data: {"type":"token", ...}` chunks ending with a `done` event.

## Run with Docker

```bash
docker compose up --build
```

Health: `curl http://localhost:8000/healthz`.

## Tests

```bash
uv run pytest -q
uv run ruff check app tests
```

The LLM is monkey-patched in `tests/conftest.py` (`litellm.acompletion`),
so tests never hit real providers.

## API contract

`POST /chat/stream` (SSE)

```json
{
  "sessionId": "uuid",
  "messages": [{"role": "user", "content": "..."}],
  "lang": "pt|en",
  "turnstileToken": "string|null"
}
```

Events:

```
data: {"type":"token","value":"…"}
data: {"type":"done","model":"…","tokens":{"prompt":N,"completion":M}}
data: {"type":"error","message":"…"}
```

`GET /healthz` → `200 {"status":"ok"}`.

## Auth flow

- First message of a session must include a valid Turnstile token.
- Backend issues an HttpOnly `chat_session` JWT (HS256, TTL 1h).
- Subsequent messages skip Turnstile but require the cookie.

## Cost / abuse defences

- `RATE_LIMIT_PER_IP` (slowapi, prefers `cf-connecting-ip`).
- `DAILY_LLM_CALL_LIMIT` daily kill-switch backed by a SQLite counter.
- Cloudflare Turnstile on the first message of every session.

## Persistence

SQLite at `DB_PATH`. Schema: `sessions`, `messages`, `daily_calls`. IPs
are hashed with `IP_HASH_SALT` before storage.

## Known follow-ups (Phase 4 — deploy is another agent)

- K3s manifests under `k8s/{prod,dev}/` (deployment, service, ingress).
- PVC for `WIKI_DIR` populated by an init container that clones
  `leoferolive/leoferolive-wiki`.
- PVC for `DB_PATH`.
- Cloudflare Tunnel ingress for `chat.leoferolive.com.br`.
- GH Actions: build + push image, kustomize apply.
