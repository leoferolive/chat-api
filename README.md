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

## Deploy

K3s on a Raspberry Pi (same cluster as `leoferolive.com.br`). Cloudflare
Tunnel terminates TLS and routes `*.leoferolive.com.br` to Traefik in the
cluster, so the ingress stays plain HTTP.

| Env | Host | Namespace | Deployment | Image |
|-----|------|-----------|------------|-------|
| prod | `chat.leoferolive.com.br` | `chat-api` | `chat-api` | `ghcr.io/leoferolive/chat-api` |
| dev | `chat-dev.leoferolive.com.br` | `chat-api-dev` | `chat-api-dev` | `ghcr.io/leoferolive/chat-api-dev` |

### Prerequisites

- K3s cluster reachable from the GitHub Actions runner via Tailscale
  (`TAILSCALE_AUTHKEY` secret) and a base64-encoded kubeconfig
  (`KUBECONFIG` secret).
- GHCR write permission (defaults to `GITHUB_TOKEN`; override via
  `GHCR_PAT` if you need cross-repo pulls). Create
  `ghcr-secret` in each namespace if the image repo is private.
- Cloudflare Tunnel hostnames `chat.leoferolive.com.br` and
  `chat-dev.leoferolive.com.br` already pointing at Traefik.
- Public wiki repo at
  `https://github.com/leoferolive/leoferolive-wiki` (init container clones
  via HTTPS; needs no PAT while public).

### First-time setup (per environment)

```bash
# 1. Create namespace + PVCs + ConfigMap + Service + Ingress
kubectl apply -f k8s/dev/namespace.yaml
kubectl apply -f k8s/dev/pvc.yaml
kubectl apply -f k8s/dev/configmap.yaml
kubectl apply -f k8s/dev/service.yaml
kubectl apply -f k8s/dev/ingress.yaml

# 2. Create the secret (NEVER commit real values)
kubectl create secret generic chat-api-secrets \
  --namespace chat-api-dev \
  --from-literal=TURNSTILE_SECRET=... \
  --from-literal=SESSION_SECRET=$(openssl rand -hex 32) \
  --from-literal=IP_HASH_SALT=$(openssl rand -hex 16) \
  --from-literal=GEMINI_API_KEY=... \
  --from-literal=OPENROUTER_API_KEY=... \
  --from-literal=ZAI_API_KEY=...

# 3. Apply the deployment (image will be tracked by GH Actions afterwards)
kubectl apply -f k8s/dev/deployment.yaml
```

For prod, swap `dev` for `prod` and `chat-api-dev` for `chat-api`.

### Deploy via GitHub Actions

- **dev (auto):** push to `main` → `ci` runs → `release.yml` cuts a stable
  tag `vX.Y.Z` and triggers `deploy-environment.yml` for `dev`.
- **dev (manual branch):** `gh workflow run deploy-branch-dev.yml -f ref=feat/foo`
  cuts an RC tag `vX.Y.Z-rc.<sha>` and deploys it.
- **prod:** `gh workflow run deploy-prod.yml -f tag=vX.Y.Z` (requires
  `production` environment approval).

Each deploy: builds an `arm64` image, pushes to GHCR, applies manifests
(skipping `secret.template.yaml`), `kubectl set image` to the new tag,
waits for rollout, then curls `/healthz` for a smoke test.

### Manual deploy (no CI)

```bash
# Build for arm64 from another machine and push:
docker buildx build --platform linux/arm64 \
  -t ghcr.io/leoferolive/chat-api-dev:manual-$(git rev-parse --short HEAD) \
  --push .

# Then on a kubeconfig-aware host:
kubectl -n chat-api-dev set image deployment/chat-api-dev \
  chat-api-dev=ghcr.io/leoferolive/chat-api-dev:manual-<sha>
kubectl -n chat-api-dev rollout status deployment/chat-api-dev
```

### Rotating a key

```bash
kubectl edit secret chat-api-secrets -n chat-api          # change the value
kubectl rollout restart deployment/chat-api -n chat-api   # pick it up
```

### Session secret rotation (automatic)

`SESSION_SECRET` rotates on a schedule via the `chat-api-rotate-session`
CronJob (manifests in `k8s/{prod,dev}/`):

- **prod:** every 90d (`0 3 1 */3 *` UTC — 03:00 on day 1 of every 3rd
  month).
- **dev:** weekly (`0 4 * * 1` UTC — Mondays at 04:00) to catch
  regressions early.

The job generates 64 hex chars from `/dev/urandom`, patches the
`chat-api-secrets` Secret, then restarts the deployment so the new value
is loaded. It runs with a dedicated `chat-api-rotator` ServiceAccount
and a `Role` scoped (via `resourceNames`) to that one Secret and that
one Deployment.

To rotate immediately without waiting for the schedule:

```bash
kubectl create job --from=cronjob/chat-api-rotate-session \
  manual-rotation-$(date +%s) -n chat-api      # or -n chat-api-dev
```

After a rotation, every previously issued `chat_session` JWT is
invalid: any visitor with an active session has to pass Turnstile again
on their next message. This is intentional — the whole point of
rotation is to limit the blast radius of a leaked secret.

### Logs

```bash
kubectl logs -n chat-api -l app=chat-api -f --tail=200
kubectl logs -n chat-api -l app=chat-api -c wiki-clone    # init container
```

### Wiki updates

The init container clones (or `git pull`s) `leoferolive-wiki` into the
`chat-api-wiki` PVC each time the pod starts. The clone mirrors the full
repo inside the volume, so the actual wiki pages live one level below the
mount point — `WIKI_DIR=/wiki`, but `index.md` and the page tree are at
`/wiki/wiki/`, alongside repo-level files (`AGENTS.md`, `README.md`,
`raw/`, …) that are **not** wiki pages. The loader auto-detects this
layout and scopes reading to `<WIKI_DIR>/wiki/`, so noise files outside
that subtree never reach the retriever.

To pick up new wiki content:

```bash
kubectl rollout restart deployment/chat-api -n chat-api
```

The running container also polls `index.md` every `WIKI_POLL_SECONDS`
(default 60s) to invalidate its in-memory cache without a restart, but a
restart is the only way to pull *new files* from git.
