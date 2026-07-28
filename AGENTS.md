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
| `uv run ruff format --check app tests` | formatting (roda no CI) |
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

## Estilo de código

Regras de estilo aplicáveis ao código Python/FastAPI deste repo. Regras de
domínio (config, LiteLLM, guard order, logging, persistência) ficam em
"Convencoes de codigo" acima — esta seção é sobre forma, não sobre negócio.

- **Funções e módulos:** funções curtas (4-20 linhas); quebre se passar
  disso. Um módulo, uma responsabilidade — os módulos de `app/` já são bem
  segmentados (`wiki_loader.py`, `retriever.py`, `llm_router.py`, `guards.py`
  etc.); mantenha esse padrão em vez de acrescentar lógica alheia a um
  módulo existente.
- **Tamanho de arquivo:** manter arquivos abaixo de 500 linhas.
  `app/main.py` (624) e `app/db.py` (538) já ultrapassam o limite — isso é
  débito conhecido, não retroativo: não é necessário quebrá-los agora, mas
  qualquer PR que toque de forma significativa um dos dois deve considerar
  extrair responsabilidades (ex.: separar rotas de `main.py`, separar
  queries de `db.py`) em vez de só engordar o arquivo.
- **Nomes:** específicos e únicos. Evitar `data`, `handler`, `manager`.
  Prefira nomes que retornem poucos hits em `grep -r` no repo.
- **Tipagem:** type hints explícitos sempre; sem `Dict`/`Any` soltos.
  Use os modelos Pydantic já existentes (`ChatRequest`, `ChatChunk`,
  `WikiPage` em `app/models.py`) como padrão — se um payload precisa de
  forma própria, crie o modelo Pydantic correspondente em vez de passar
  `dict` cru entre camadas.
- **Sem duplicação; early return.** Extraia lógica repetida para função ou
  módulo. Prefira `if not x: return` a `if x: ...` aninhado — máximo 2
  níveis de indentação por função.
- **Mensagens de exceção:** sempre incluir o valor recebido e o shape
  esperado, por exemplo:

  ```python
  raise ValueError(
      f"model string invalido: {model!r}; esperado formato "
      f"'<provider>/<model>', ex. 'gemini/gemini-2.5-flash'"
  )
  ```

  **Exceção:** para secrets (Turnstile, session JWT, API keys dos providers)
  ou conteúdo de mensagem de chat, não incluir o valor recebido na exceção —
  esses `str(exc)` acabam em log de produção (ex. `app/main.py`) e violam a
  política de "nao logar conteudo de mensagens"/PII de "Convencoes de
  codigo". Descreva o formato esperado sem ecoar o valor.

## Comentários

- Preservar comentários existentes em refactors — carregam intenção e
  proveniência; não apagar sem necessidade.
- Escrever o PORQUÊ, não o QUÊ (pule `# incrementa contador` acima de
  `i += 1`).
- Docstring em função pública: intenção + um exemplo de uso.
- Referenciar issue/commit quando a linha existir por causa de um bug
  específico ou limitação de upstream (ex. workaround do LiteLLM).

## Testes (estilo)

Comandos já documentados em "Sempre" e "Comandos" (`uv run pytest -q`,
`uv run ruff check app tests`) — aqui só as regras de estilo:

- Toda função nova ganha teste; todo bugfix ganha teste de regressão.
- Mock de I/O externo (API, DB, filesystem) via classe fake nomeada, não
  stub inline solto — siga o padrão já usado para o LiteLLM, monkey-patched
  em `tests/conftest.py`.
- Testes devem ser F.I.R.S.T.: fast, independent, repeatable,
  self-validating, timely.

## Dependências

- Injeção de dependências é via `Depends()` do FastAPI — é o equivalente
  idiomático a "injetar por construtor/parâmetro" neste projeto. Config via
  `pydantic-settings` (`app/config.py`) é o padrão idiomático do framework
  e é uma exceção aceita, não uma violação dessa regra.
- Wrapper fino sobre lib de terceiros: `app/llm_router.py` já cumpre esse
  papel para o LiteLLM — siga o mesmo padrão para qualquer outra
  dependência externa relevante em vez de espalhar chamadas diretas à lib
  pelo código.

## Estrutura

Layout já documentado na seção "Layout" acima e já segue a convenção
FastAPI (models/config/routers separados) — mantenha essa separação ao
adicionar código novo em vez de concentrar tudo em `main.py`.

## Formatting

- Formatador padrão: `ruff format` (config em `pyproject.toml`,
  `[tool.ruff]`). Não discutir estilo além disso.
- `uv run ruff format --check app tests` — roda no CI (`.github/workflows/
  ci.yml`); rode antes de commit.
- `uv run ruff format app tests` — aplica a formatação.

## Logging (estilo)

Ver "Convencoes de codigo" acima para a política completa (structlog JSON,
sem PII). Existe uma CLI hoje: `app/judge/cli.py`, invocada pelos CronJobs
`k8s/{prod,dev}/cronjob-judge.yaml` (`python -m app.judge.cli --once
--limit N`). Ela já configura `structlog` JSON para logs operacionais — o
`print(summary)` final, que imprime o resultado do batch para quem lê o log
do Job manualmente, é o caso legítimo de "texto plano só para saída de CLI
voltada a humano": não convertê-lo para JSON.

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

## Session secret rotation

`SESSION_SECRET` is rotated automatically by a `CronJob`
(`chat-api-rotate-session`) in each namespace. Manifests live in
`k8s/{prod,dev}/{sa,role,rolebinding,cronjob}-rotate-session.yaml`.

- **Schedule:** prod every 90d (`0 3 1 */3 *` UTC, day 1 of every 3rd
  month at 03:00); dev weekly (`0 4 * * 1` UTC, Mondays 04:00) so
  regressions surface fast.
- **What it does:** generates a fresh 64-char hex via `/dev/urandom`,
  patches `chat-api-secrets` with `kubectl patch --type=merge`, then
  `kubectl rollout restart deployment/chat-api[-dev]` and waits for
  rollout (timeout 120s).
- **Permissions:** dedicated `chat-api-rotator` ServiceAccount, with a
  `Role` scoped via `resourceNames` to exactly the secret and deployment
  for that namespace (no wildcard access).
- **Rotate manually:**

  ```bash
  kubectl create job --from=cronjob/chat-api-rotate-session \
    manual-rotation-$(date +%s) -n chat-api      # or chat-api-dev
  ```

- **Side effect:** every previously issued `chat_session` JWT becomes
  invalid. Active visitors must pass Turnstile again on their next
  message. Acceptable — this is the whole point.

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
