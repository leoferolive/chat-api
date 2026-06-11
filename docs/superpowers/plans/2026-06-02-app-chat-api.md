# Dashboard chat-api (métricas de negócio) — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Acrescentar 3 métricas de **negócio** ao chat-api (sessões, usuários únicos e mensagens do dia UTC) — expostas como gauges Prometheus alimentados por um background task que faz `COUNT` barato no SQLite a cada ~60s — e adicionar 3 painéis correspondentes ao dashboard Grafana existente, **sem remover** os 8 painéis operacionais atuais.

**Architecture:** A instrumentação fica no repo `chat-api` (Python/FastAPI). Três `Gauge` globais são definidos em `app/metrics.py` (registrados uma única vez no import). Três métodos `count_*_today()` na classe `Database` reutilizam a **conexão async única** já mantida em `db._conn` (sem abrir conexão nova, sem vazamento). Um coletor assíncrono (`app/business_metrics.py`) roda em loop dentro do lifespan: a cada 60s consulta os counts e seta os gauges; é cancelado no shutdown. O dashboard (repo `chat-api-monitoring`) ganha uma linha de 3 painéis em `y: 32` (abaixo dos 8 existentes que ocupam `y: 0..31`), preservando o JSON atual.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, prometheus-client, pytest + pytest-asyncio (asyncio_mode=auto), ruff. Grafana dashboard-as-code (JSON provisionado), datasource Prometheus uid `prometheus`.

---

## Convenções deste plano

- **Regra global (worktrees):** todo o trabalho de cada repo acontece numa **git worktree dedicada**, mantendo o workspace principal limpo. Veja a Task 0.
- **Dois repos:** instrumentação em `/home/leoferolive/projetos/chat-api`; dashboard em `/home/leoferolive/projetos/chat-api-monitoring`. Os caminhos abaixo são do **workspace principal**; dentro de uma worktree, substitua a raiz pelo diretório da worktree.
- **TDD:** para cada unidade de código: escrever teste falhando → rodar e ver falhar → implementação mínima → rodar e ver passar → commit.
- **Cardinalidade:** gauges sem labels (valores agregados do dia). Nada de IDs de usuário como label.
- **Janela do dia (UTC):** reutilizar o padrão já presente em `app/db.py:190-192` —
  `datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()` —
  porque `created_at` é gravado em **epoch seconds** (`_now_ts()` em `app/db.py:42-43`).
- **Identidade de "usuário único":** o schema de `sessions` (`app/db.py:14-21`) não tem user-id;
  `user_name` é nullable e opcional. A identidade estável é `ip_hash`. Portanto
  **usuários únicos hoje = `COUNT(DISTINCT ip_hash)`** das sessões criadas hoje (excluindo `''`/NULL).

---

## File Structure (mapa de mudanças)

**Repo `chat-api`:**
- Modify `app/metrics.py` — adicionar 3 gauges (`chat_api_sessions_today`, `chat_api_unique_users_today`, `chat_api_messages_today`).
- Modify `app/db.py` — adicionar 3 métodos async de COUNT reutilizando `self._conn`.
- Create `app/business_metrics.py` — coletor assíncrono (loop 60s) + helper de "uma passada" testável.
- Modify `app/main.py` — iniciar/cancelar o background task no lifespan.
- Modify `tests/test_db.py` — testes dos 3 counts.
- Create `tests/test_business_metrics.py` — teste do coletor (uma passada seta os gauges).
- Modify `tests/test_metrics.py` — teste de que os 3 gauges aparecem em `/metrics`.

**Repo `chat-api-monitoring`:**
- Modify `k8s/monitoring/dashboards/chat-api.json` — adicionar 3 painéis em `y: 32`, bump `version`.

---

## Task 0: Preparar worktrees (regra global)

**Files:** nenhum arquivo de código — apenas setup de git.

- [ ] **Step 1: Criar worktree do repo `chat-api`**

REQUIRED SUB-SKILL: superpowers:using-git-worktrees.

```bash
git -C /home/leoferolive/projetos/chat-api worktree add -b feat/business-metrics ../chat-api-business-metrics
```
Esperado: `Preparing worktree ... HEAD is now at <sha>`. A worktree fica em
`/home/leoferolive/projetos/chat-api-business-metrics`.

- [ ] **Step 2: Criar worktree do repo `chat-api-monitoring`**

```bash
git -C /home/leoferolive/projetos/chat-api-monitoring worktree add -b feat/chat-api-business-panels ../chat-api-monitoring-business-panels
```
Esperado: worktree em `/home/leoferolive/projetos/chat-api-monitoring-business-panels`.

> A partir daqui, os comandos `pytest`/`ruff`/`git` rodam **dentro da worktree correspondente**.
> Os caminhos absolutos nos blocos de código abaixo referem-se ao **repo principal**;
> ao editar, aplique no arquivo equivalente dentro da worktree.

- [ ] **Step 3: Confirmar baseline verde na worktree do `chat-api`**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest -q
```
Esperado: toda a suíte passa (verde) antes de qualquer mudança. Se `uv` não estiver
disponível, use `python -m pytest -q` no venv do projeto.

---

## Task 1: Gauges de negócio em `app/metrics.py`

**Files:**
- Modify: `app/metrics.py` (adicionar após o gauge `DAILY_CALLS`, antes de `ROUTER_OUTCOME_TOTAL`)
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Escrever o teste falhando**

Adicionar ao final de `tests/test_metrics.py`:

```python
@pytest.mark.asyncio
async def test_business_gauges_exposed_in_metrics(client) -> None:
    """Os 3 gauges de negócio devem aparecer no payload /metrics."""
    resp = await client.get("/metrics", headers={"host": "127.0.0.1"})
    assert resp.status_code == 200
    body = resp.text
    assert "# TYPE chat_api_sessions_today gauge" in body
    assert "# TYPE chat_api_unique_users_today gauge" in body
    assert "# TYPE chat_api_messages_today gauge" in body
```

- [ ] **Step 2: Rodar o teste e ver falhar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_metrics.py::test_business_gauges_exposed_in_metrics -v
```
Esperado: FAIL — o body não contém `# TYPE chat_api_sessions_today gauge` (gauges ainda
não definidos, logo nem aparecem em `/metrics`).

- [ ] **Step 3: Implementar os gauges**

Em `app/metrics.py`, inserir logo após o bloco `DAILY_CALLS` (linhas 53-56) e antes de
`ROUTER_OUTCOME_TOTAL`:

```python
# --- Business gauges (polled from SQLite by a background task) ----------
# Sem labels: cada um carrega um único valor agregado do dia (UTC). São
# definidos no import (registro único na default registry) e setados pelo
# coletor em app/business_metrics.py — nunca reinstanciados em runtime.
SESSIONS_TODAY = Gauge(
    "chat_api_sessions_today",
    "Distinct chat sessions created today (UTC).",
)

UNIQUE_USERS_TODAY = Gauge(
    "chat_api_unique_users_today",
    "Distinct users (by hashed IP) seen today (UTC).",
)

MESSAGES_TODAY = Gauge(
    "chat_api_messages_today",
    "Chat messages stored today (UTC), both roles.",
)
```

- [ ] **Step 4: Rodar o teste e ver passar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_metrics.py::test_business_gauges_exposed_in_metrics -v
```
Esperado: PASS. (Os gauges aparecem em `/metrics` zerados assim que importados.)

- [ ] **Step 5: Lint + commit**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics
uv run ruff check app/metrics.py
git add app/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): add business gauges (sessions/users/messages today)"
```
Esperado: ruff sem erros; commit criado.

---

## Task 2: Métodos de COUNT em `app/db.py`

Reutilizam a conexão única `self._conn` (sem abrir conexão nova). Counts baratos:
um `COUNT` por tabela, filtrando por `created_at >= day_start` (epoch UTC).

**Files:**
- Modify: `app/db.py` (adicionar métodos ao final da classe `Database`, após
  `count_calls_today_by_ip`, ~linha 202)
- Test: `tests/test_db.py`

- [ ] **Step 1: Escrever o teste falhando**

Adicionar ao final de `tests/test_db.py`:

```python
@pytest.mark.asyncio
async def test_business_counts_today(tmp_path: Path) -> None:
    db = Database(tmp_path / "b.sqlite")
    await db.connect()
    try:
        # Duas sessões de dois IPs distintos, criadas "hoje".
        await db.upsert_session("s1", "iphash-A", "pt")
        await db.upsert_session("s2", "iphash-B", "pt")
        # Terceira sessão reusa um IP já visto -> não conta como user único novo.
        await db.upsert_session("s3", "iphash-A", "en")

        await db.save_turn(session_id="s1", role="user", content="oi")
        await db.save_turn(session_id="s1", role="assistant", content="ola")
        await db.save_turn(session_id="s2", role="user", content="hi")

        assert await db.count_sessions_today() == 3
        assert await db.count_unique_users_today() == 2
        assert await db.count_messages_today() == 3
    finally:
        await db.close()
```

- [ ] **Step 2: Rodar o teste e ver falhar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_db.py::test_business_counts_today -v
```
Esperado: FAIL — `AttributeError: 'Database' object has no attribute 'count_sessions_today'`.

- [ ] **Step 3: Implementar os métodos**

Em `app/db.py`, adicionar um helper de borda de dia e os 3 métodos ao final da classe
`Database` (depois de `count_calls_today_by_ip`). Inserir o helper de módulo logo abaixo
de `_today_key` (linha 47):

```python
def _utc_day_start_ts() -> int:
    """Epoch (s) do início do dia atual em UTC — mesma truncagem usada nos
    limites por-IP (ver count_calls_today_by_ip)."""
    return int(
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
```

E os métodos (ao final da classe `Database`):

```python
    async def count_sessions_today(self) -> int:
        """Distinct sessions created today (UTC)."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE created_at >= ?",
            (_utc_day_start_ts(),),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_unique_users_today(self) -> int:
        """Distinct hashed IPs across sessions created today (UTC)."""
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT COUNT(DISTINCT ip_hash) FROM sessions
            WHERE created_at >= ? AND ip_hash IS NOT NULL AND ip_hash <> ''
            """,
            (_utc_day_start_ts(),),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_messages_today(self) -> int:
        """Messages stored today (UTC), both roles."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE created_at >= ?",
            (_utc_day_start_ts(),),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
```

> Nota de robustez: `count_unique_users_today` exclui `ip_hash` vazio/NULL para não inflar
> a contagem com sessões sem IP (ex.: testes ou edge cases). `created_at` é epoch seconds,
> então o filtro `>= _utc_day_start_ts()` casa exatamente com a gravação de `_now_ts()`.

- [ ] **Step 4: Rodar o teste e ver passar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_db.py::test_business_counts_today -v
```
Esperado: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics
uv run ruff check app/db.py
git add app/db.py tests/test_db.py
git commit -m "feat(db): add cheap today COUNT queries (sessions/users/messages)"
```
Esperado: ruff sem erros; commit criado.

---

## Task 3: Coletor assíncrono `app/business_metrics.py`

Separa **a passada de coleta** (testável, sem `sleep`) do **loop** (com `sleep`).
Isso evita timers reais nos testes e mantém a função de coleta barata e isolada.

**Files:**
- Create: `app/business_metrics.py`
- Test: `tests/test_business_metrics.py`

- [ ] **Step 1: Escrever o teste falhando**

Criar `tests/test_business_metrics.py`:

```python
"""Tests for the business-metrics collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.business_metrics import collect_once
from app.db import Database
from app.metrics import MESSAGES_TODAY, SESSIONS_TODAY, UNIQUE_USERS_TODAY


@pytest.mark.asyncio
async def test_collect_once_populates_gauges(tmp_path: Path) -> None:
    db = Database(tmp_path / "c.sqlite")
    await db.connect()
    try:
        await db.upsert_session("s1", "iphash-A", "pt")
        await db.upsert_session("s2", "iphash-B", "pt")
        await db.save_turn(session_id="s1", role="user", content="oi")

        await collect_once(db)

        assert SESSIONS_TODAY._value.get() == 2
        assert UNIQUE_USERS_TODAY._value.get() == 2
        assert MESSAGES_TODAY._value.get() == 1
    finally:
        await db.close()
```

> `Gauge._value.get()` lê o valor atual do gauge — padrão usado por prometheus-client
> em testes para inspecionar samples sem fazer scrape.

- [ ] **Step 2: Rodar o teste e ver falhar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_business_metrics.py -v
```
Esperado: FAIL — `ModuleNotFoundError: No module named 'app.business_metrics'`.

- [ ] **Step 3: Implementar o coletor**

Criar `app/business_metrics.py`:

```python
"""Background collector that polls SQLite and feeds the business gauges.

`collect_once` does a single cheap pass (three COUNTs) and sets the gauges —
it is unit-tested directly. `run_collector` loops it on a fixed interval and
is started/cancelled by the FastAPI lifespan. The collector reuses the single
shared aiosqlite connection held by `Database` (no new connection per pass, no
leak). Errors in one pass are logged and swallowed so the loop never dies.
"""

from __future__ import annotations

import asyncio

import structlog

from .db import Database
from .metrics import MESSAGES_TODAY, SESSIONS_TODAY, UNIQUE_USERS_TODAY

log = structlog.get_logger("chat-api.business_metrics")

DEFAULT_INTERVAL_SECONDS = 60


async def collect_once(db: Database) -> None:
    """One cheap pass: three COUNTs over today's window, set the gauges."""
    SESSIONS_TODAY.set(await db.count_sessions_today())
    UNIQUE_USERS_TODAY.set(await db.count_unique_users_today())
    MESSAGES_TODAY.set(await db.count_messages_today())


async def run_collector(
    db: Database, interval_seconds: int = DEFAULT_INTERVAL_SECONDS
) -> None:
    """Loop `collect_once` forever. Cancelled by the lifespan on shutdown."""
    while True:
        try:
            await collect_once(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            log.warning("business_metrics_collect_failed", err=str(exc))
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: Rodar o teste e ver passar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_business_metrics.py -v
```
Esperado: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics
uv run ruff check app/business_metrics.py tests/test_business_metrics.py
git add app/business_metrics.py tests/test_business_metrics.py
git commit -m "feat(metrics): add async business-metrics collector (60s poll)"
```
Esperado: ruff sem erros; commit criado.

---

## Task 4: Registrar o coletor no lifespan (`app/main.py`)

Iniciar o task após `db.connect()` e fazer uma coleta imediata (gauges populados já no
startup, sem esperar 60s). Cancelar o task no `finally` antes de fechar a conexão.

**Files:**
- Modify: `app/main.py` (imports ~32-40 e lifespan 78-99)
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Escrever o teste falhando**

Adicionar a `tests/test_metrics.py` (usa o fixture `client`, que dispara o lifespan real):

```python
@pytest.mark.asyncio
async def test_business_gauges_populated_after_chat(client, mock_llm) -> None:
    """Após um chat, o coletor (rodado no startup + on-demand) reflete dados reais."""
    # Provoca uma sessão + mensagens reais.
    resp = await client.post(
        "/chat/stream",
        json={
            "sessionId": "biz-sess-1",
            "lang": "pt",
            "messages": [{"role": "user", "content": "quem e o leonardo?"}],
        },
        headers={"host": "127.0.0.1"},
    )
    assert resp.status_code == 200
    _ = resp.text  # drena o stream SSE

    # Força uma coleta determinística (não dependemos do timer de 60s no teste).
    from app.business_metrics import collect_once

    await collect_once(client.app.state.db)

    body = (await client.get("/metrics", headers={"host": "127.0.0.1"})).text
    assert metric_value(body, "chat_api_sessions_today") >= 1.0
    assert metric_value(body, "chat_api_messages_today") >= 1.0
```

- [ ] **Step 2: Rodar o teste e ver falhar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_metrics.py::test_business_gauges_populated_after_chat -v
```
Esperado: PASS já é possível (porque o teste chama `collect_once` explicitamente). Para
garantir que o **startup** também dispara a coleta e que o task é registrado/cancelado sem
vazar, prosseguir para o registro no lifespan (Steps 3-4) — e então rodar a suíte inteira
no Step 5, que falharia se o task vazasse uma task pendente ("Task was destroyed but it is
pending"). Se este teste isolado já passar, trate como verde e siga.

- [ ] **Step 3: Implementar o registro no lifespan**

Em `app/main.py`, adicionar o import (junto aos demais imports de `app`, ~linha 31):

```python
from .business_metrics import collect_once, run_collector
```

Substituir o corpo do `lifespan` (linhas 78-99) por:

```python
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

    # Popular os gauges de negócio já no startup, depois deixar o coletor
    # rodando em background (poll de 60s). Reaproveita a conexão única do db.
    await collect_once(db)
    metrics_task = asyncio.create_task(run_collector(db))

    log.info("startup", env=settings.env, wiki_dir=str(settings.wiki_dir))
    try:
        yield
    finally:
        metrics_task.cancel()
        try:
            await metrics_task
        except asyncio.CancelledError:
            pass
        await db.close()
        log.info("shutdown")
```

> `asyncio` já está importado em `app/main.py:5`. O `cancel()` + `await` garante que o
> loop pare **antes** de `db.close()`, evitando uma coleta correr sobre conexão fechada e
> evitando "Task was destroyed but it is pending".

- [ ] **Step 4: Rodar o teste e ver passar**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest tests/test_metrics.py::test_business_gauges_populated_after_chat -v
```
Esperado: PASS.

- [ ] **Step 5: Rodar a suíte inteira (regressão + sem task vazada)**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest -q
```
Esperado: toda a suíte verde, **sem** warnings de "Task was destroyed but it is pending"
(prova de que o coletor é cancelado limpo no shutdown do lifespan).

- [ ] **Step 6: Lint + commit**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics
uv run ruff check app/main.py tests/test_metrics.py
git add app/main.py tests/test_metrics.py
git commit -m "feat(main): start business-metrics collector in lifespan"
```
Esperado: ruff sem erros; commit criado.

---

## Task 5: Painéis de negócio no dashboard (`chat-api.json`)

Adicionar 3 painéis numa linha nova em `y: 32` (os 8 atuais ocupam `y: 0..31`,
4 linhas de `h: 8`). Layout: 3 painéis lado a lado em `w: 8` (8+8+8 = 24, full width).
Sessões e usuários como `stat`; mensagens como `timeseries` (curva ao longo do dia).
IDs 9, 10, 11 (os existentes vão de 1 a 8). Não remover nada.

**Files:**
- Modify: `k8s/monitoring/dashboards/chat-api.json` (array `panels`, e `version`)

> Este Task roda na **worktree do `chat-api-monitoring`**:
> `/home/leoferolive/projetos/chat-api-monitoring-business-panels`.

- [ ] **Step 1: Inserir os 3 painéis**

No `k8s/monitoring/dashboards/chat-api.json`, dentro do array `"panels"`, **após** o
painel de id 8 (que termina na linha ~147, o `}` antes do `]` que fecha `panels`),
adicionar uma vírgula após esse `}` e colar os 3 objetos abaixo:

```json
    {
      "datasource": {"type": "prometheus", "uid": "prometheus"},
      "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": null}]}}},
      "gridPos": {"h": 8, "w": 8, "x": 0, "y": 32},
      "id": 9,
      "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [
        {"expr": "max(chat_api_sessions_today{namespace=~\"$namespace\"})", "refId": "A"}
      ],
      "title": "Conversas (sessions) hoje",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "prometheus"},
      "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}]}}},
      "gridPos": {"h": 8, "w": 8, "x": 8, "y": 32},
      "id": 10,
      "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "textMode": "auto"},
      "targets": [
        {"expr": "max(chat_api_unique_users_today{namespace=~\"$namespace\"})", "refId": "A"}
      ],
      "title": "Usuarios unicos hoje",
      "type": "stat"
    },
    {
      "datasource": {"type": "prometheus", "uid": "prometheus"},
      "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "palette-classic"}}},
      "gridPos": {"h": 8, "w": 8, "x": 16, "y": 32},
      "id": 11,
      "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "single"}},
      "targets": [
        {"expr": "max(chat_api_messages_today{namespace=~\"$namespace\"})", "legendFormat": "mensagens hoje", "refId": "A"}
      ],
      "title": "Mensagens hoje",
      "type": "timeseries"
    }
```

> Os 3 usam `max(...{namespace=~"$namespace"})` para consolidar réplicas/instâncias num
> único valor (idêntico ao painel "Chamadas LLM hoje", id 2, que já usa `max(...)`).
> A variável de template `$namespace` já existe no dashboard (`templating.list`).

- [ ] **Step 2: Bump da versão do dashboard**

No mesmo arquivo, alterar `"version": 1` para `"version": 2` (linha ~174).

- [ ] **Step 3: Validar que o JSON é válido**

```bash
cd /home/leoferolive/projetos/chat-api-monitoring-business-panels
python -c "import json,sys; d=json.load(open('k8s/monitoring/dashboards/chat-api.json')); ids=[p['id'] for p in d['panels']]; print('panels:', len(d['panels']), 'ids:', ids); assert len(d['panels'])==11; assert ids==[1,2,3,4,5,6,7,8,9,10,11]; assert d['version']==2; print('OK')"
```
Esperado: `panels: 11 ids: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]` seguido de `OK`.
(Falha aqui = vírgula faltando/sobrando ou painel removido por engano.)

- [ ] **Step 4: Verificar que nenhum gridPos sobrepõe**

```bash
cd /home/leoferolive/projetos/chat-api-monitoring-business-panels
python -c "
import json
d=json.load(open('k8s/monitoring/dashboards/chat-api.json'))
cells=set()
for p in d['panels']:
    g=p['gridPos']
    for xx in range(g['x'], g['x']+g['w']):
        for yy in range(g['y'], g['y']+g['h']):
            key=(xx,yy)
            assert key not in cells, f'overlap at {key} (panel id {p[\"id\"]})'
            cells.add(key)
print('no overlap; rows used y=0..%d' % (max(p['gridPos']['y']+p['gridPos']['h'] for p in d['panels'])-1))
"
```
Esperado: `no overlap; rows used y=0..39` (8 painéis em y 0..31 + 3 novos em y 32..39).

- [ ] **Step 5: Commit**

```bash
cd /home/leoferolive/projetos/chat-api-monitoring-business-panels
git add k8s/monitoring/dashboards/chat-api.json
git commit -m "feat(dashboard): add chat-api business panels (sessions/users/messages today)"
```
Esperado: commit criado.

---

## Task 6: Verificação final e handoff

**Files:** nenhum — verificação.

- [ ] **Step 1: Suíte completa do `chat-api` verde**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run pytest -q
```
Esperado: tudo verde, sem warnings de task pendente.

- [ ] **Step 2: Lint completo**

```bash
cd /home/leoferolive/projetos/chat-api-business-metrics && uv run ruff check app tests
```
Esperado: `All checks passed!`.

- [ ] **Step 3: Smoke manual de `/metrics` (opcional, fora de container)**

Se quiser confirmar o payload localmente, suba a app com um DB temporário e cheque que os
3 nomes aparecem. Caso contrário, os testes da Task 1 e 4 já cobrem isso.

- [ ] **Step 4: (Após deploy) Validar o dashboard no Grafana**

REQUIRED: usar o CLI `browser-use` (regra global — **não** usar Playwright MCP).
Abrir `https://grafana.leoferolive.com.br`, navegar ao dashboard `chat-api`, confirmar que:
- os 8 painéis operacionais continuam presentes;
- a linha nova mostra "Conversas (sessions) hoje", "Usuarios unicos hoje", "Mensagens hoje"
  **com dados não-vazios** (após o coletor rodar ≥1x em prod);
- tirar 1 screenshot de evidência.

- [ ] **Step 5: Finalizar branches**

REQUIRED SUB-SKILL: superpowers:finishing-a-development-branch (decidir merge/PR por repo).
Há **2 branches** (uma por repo): `feat/business-metrics` (chat-api) e
`feat/chat-api-business-panels` (chat-api-monitoring). Abrir/mergear conforme o fluxo do
usuário. Remover as worktrees quando concluído:

```bash
git -C /home/leoferolive/projetos/chat-api worktree remove ../chat-api-business-metrics
git -C /home/leoferolive/projetos/chat-api-monitoring worktree remove ../chat-api-monitoring-business-panels
```

---

## Notas de risco / decisões

- **Counts baratos:** cada gauge é um único `COUNT` com filtro `created_at >= day_start`.
  Sem índice em `created_at`, é um scan da tabela do dia — trivial no volume do chat-api.
  Se algum dia crescer, criar índice `CREATE INDEX ... ON messages(created_at)` (YAGNI agora).
- **Sem vazamento de conexão:** o coletor **não abre conexão**; chama métodos de `Database`
  que usam `self._conn` (a conexão única do app). O task é cancelado e aguardado no
  `finally` do lifespan, antes de `db.close()`.
- **Gauges globais:** definidos uma vez no import de `app/metrics.py`. O coletor só faz
  `.set()` — nunca re-registra. Re-importar o módulo nos testes não duplica (default
  registry dedup por nome no import único).
- **"Usuários únicos" = IP distinto:** escolha forçada pelo schema (não há user-id estável;
  `user_name` é opcional). Documentado acima e refletido na query e no título do painel.
