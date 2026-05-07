# chat-api · AGENTS.md

Backend para o chat com IA do `leoferolive.com.br`. FastAPI + LiteLLM,
deploy K3s no Raspberry Pi (mesmo cluster do site).

## Sempre

- Python 3.12, gerenciado por `uv` (`pyproject.toml` é a fonte da verdade).
- `ruff check app tests` zero warnings antes de commit.
- `pytest -q` verde (LiteLLM monkey-patched em `tests/conftest.py` — testes
  nunca chamam provider real).
- Conventional commits (`feat:`, `fix:`, `chore:`, `test:`, `docs:`).
- Toda chave secreta passa **só** pelo `Secret` do K8s. Nada de chave em
  `ConfigMap`, `.env` commitado, manifest YAML real ou log.

## Comandos

| Comando | Quando |
|---|---|
| `uv sync --all-extras` | bootstrap deps |
| `uv run uvicorn app.main:app --reload` | dev local (porta 8000) |
| `uv run pytest -q` | suite de testes |
| `uv run ruff check app tests` | lint |
| `docker compose up --build` | smoke test do container |

## Layout

```
app/
  main.py          FastAPI app, /chat/stream, /healthz, lifespan
  config.py        pydantic-settings (env)
  models.py        ChatRequest / ChatChunk / WikiPage
  wiki_loader.py   le WIKI_DIR, parse index.md, polling cache
  retriever.py     keyword-overlap scorer (v1)
  prompt.py        persona PT/EN + bloco de contexto da wiki
  llm_router.py    litellm.acompletion(stream=True) + fallback
  guards.py        Turnstile, slowapi, cost gate, session JWT
  db.py            aiosqlite (sessions / messages / daily_calls)
  sse.py           EventSourceResponse helper
tests/             pytest + httpx (LiteLLM mockado)
wiki-fixture/      wiki minima usada em dev e testes
k8s/{prod,dev}/    manifests K3s
.github/workflows/ ci + deploy-environment + deploy-prod + deploy-branch-dev + release
```

## Convencoes de codigo

- Toda config nova entra em `app/config.py` (pydantic-settings) + em
  `.env.example` + (se for nao-secreta) no `ConfigMap` de cada ambiente.
- LiteLLM model strings: prefixo do provider obrigatorio
  (`gemini/gemini-2.5-flash`, `openrouter/anthropic/claude-haiku-4.5`,
  `zai/glm-4.7-flash`). O prefixo `zai/` e tratado em `llm_router.py` como
  OpenAI-compatible com `api_base = ZAI_BASE_URL`.
- Guard order em `/chat/stream`: Turnstile (1a msg) → slowapi rate limit
  → cost gate diario → handler.
- Logs: `structlog` JSON em prod. Nao logar conteudo de mensagens; apenas
  metadados (session_id, model, latency_ms, prompt_tokens,
  completion_tokens).
- Persistencia: SQLite em `DB_PATH` (volume PVC em K8s). IPs sempre
  hashed com `IP_HASH_SALT` antes de gravar.

## Deploy architecture

Espelha o site (`leoferolive.com.br`). Decisoes:

- **Manifests:** plain YAML em `k8s/{prod,dev}/` (sem Helm, sem Kustomize,
  igual ao site). Imagem fixada `:latest` no manifest, atualizada em
  cada deploy via `kubectl set image` para a tag exata
  (`ghcr.io/leoferolive/chat-api[-dev]:vX.Y.Z` ou `:vX.Y.Z-rc.<sha>`).
- **Namespaces:** `chat-api` (prod) e `chat-api-dev` (dev). Isolados do
  namespace do site.
- **Hosts:** `chat.leoferolive.com.br` (prod), `chat-dev.leoferolive.com.br`
  (dev). Cloudflare Tunnel ja roteia `*.leoferolive.com.br` → Traefik;
  nao precisa registrar hostname novo no tunnel (se for ALB explicito,
  ver `docs/deploy-guide` do site).
- **Imagens:** GHCR `ghcr.io/leoferolive/chat-api` e
  `ghcr.io/leoferolive/chat-api-dev`. Build em runner `ubuntu-24.04-arm`
  para nativo arm64 (Raspberry Pi).
- **Wiki:** PVC `chat-api-wiki` (5Gi). Init container `wiki-clone` (imagem
  `alpine/git`) faz `git clone --depth 1` no primeiro start e
  `git pull --depth 1 --ff-only` em starts subsequentes. Repositorio
  publico em `https://github.com/leoferolive/leoferolive-wiki`. O clone
  espelha o repo *inteiro* dentro do volume (`WIKI_DIR=/wiki`), entao
  `index.md` e as paginas vivem em `/wiki/wiki/` (ao lado de `AGENTS.md`,
  `README.md`, `raw/` etc., que NAO sao paginas). O `wiki_loader` detecta
  esse layout e escopa a leitura para `<WIKI_DIR>/wiki/` automaticamente.
- **DB:** PVC `chat-api-db` (1Gi). Mounta em `/data`. Sem backup
  automatizado por enquanto (TODO em Fase 5).
- **Recursos:** 100m/256Mi requests, 500m/512Mi limits. 1 replica
  (Raspberry Pi).
- **Strategy:** `Recreate` (PVCs sao RWO; nao queremos dois pods
  competindo pela DB).

## Workflows

| Workflow | Trigger | Faz |
|---|---|---|
| `ci.yml` | push/PR em main | ruff + pytest |
| `release.yml` | `ci` success em main | tag `vX.Y.Z` + GitHub Release + dev deploy |
| `deploy-branch-dev.yml` | manual `gh workflow run -f ref=<ref>` | tag `vX.Y.Z-rc.<sha>` + dev deploy |
| `deploy-prod.yml` | manual `gh workflow run -f tag=vX.Y.Z` | approval gate `production` + prod deploy |
| `deploy-environment.yml` | reusable | build arm64 → push GHCR → kubectl apply → set image → rollout → smoke /healthz |

## Atualizacao da wiki

Workflow Karpathy LLM-Wiki:

1. Editar paginas em `leoferolive-wiki/` localmente (Claude Code para
   ingest/lint).
2. `git push` em `leoferolive-wiki` (main).
3. `kubectl rollout restart deployment/chat-api -n chat-api` para forcar
   o init container a fazer `git pull` e atualizar o PVC.
4. O `wiki_loader` tambem reavalia o hash de `index.md` a cada
   `WIKI_POLL_SECONDS` (60s default), entao mudancas em paginas existentes
   (sem novos arquivos) podem ser pegas sem restart.

## Secrets esperados (out-of-band)

Por namespace (`chat-api` e `chat-api-dev`):

```bash
kubectl create secret generic chat-api-secrets \
  -n <ns> \
  --from-literal=TURNSTILE_SECRET=... \
  --from-literal=SESSION_SECRET=$(openssl rand -hex 32) \
  --from-literal=IP_HASH_SALT=$(openssl rand -hex 16) \
  --from-literal=GEMINI_API_KEY=... \
  --from-literal=OPENROUTER_API_KEY=... \
  --from-literal=ZAI_API_KEY=...
```

Secrets do GitHub Actions (settings → secrets and variables → actions):

- `KUBECONFIG` — base64 do kubeconfig com permissao em ambos namespaces
- `TAILSCALE_AUTHKEY` — auth key reusable para Tailscale entrar na rede
- `GHCR_PAT` (opcional) — PAT com `write:packages` se o `GITHUB_TOKEN`
  default nao bastar

## Definition of Done (Fase 4)

- `kubectl apply -f k8s/dev/*.yaml` (exceto secret.template) sobe sem erro.
- Pod healthy em < 30s. Init container loga 1 commit do wiki repo.
- `curl https://chat-dev.leoferolive.com.br/healthz` retorna 200.
- `POST /chat/stream` retorna SSE valido.
- Logs JSON visiveis em `kubectl logs`.
- Smoke test do workflow passa.

## Nao-objetivos

- Helm / Kustomize / ArgoCD (overkill enquanto for um servico).
- Backup automatizado da DB SQLite (Fase 5).
- HPA / multi-replica (PVC RWO impede; e o Pi nao aguenta mesmo).
- mTLS interno (Cloudflare Tunnel cobre o perimetro).
