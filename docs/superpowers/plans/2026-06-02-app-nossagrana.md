# Dashboard nossagrana — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instrumentar o `apps/api` do **nossagrana** com `/metrics` (Prometheus) e entregar, no repo `chat-api-monitoring`, um ServiceMonitor + dashboard Grafana (`dashboards/nossagrana.json`) com painéis de negócio (SQL via datasource Postgres) e operacionais (Prometheus), mais um PrometheusRule opcional com 2 alertas.

**Architecture:** A app expõe `GET /metrics` (sem auth, fora do rate-limit, sem labels de alta cardinalidade). Um ServiceMonitor faz scrape do Service `nossagrana-api` (port `http`, path `/metrics`, 30s). O Prometheus do `kube-prometheus-stack` armazena. O Grafana provisiona o dashboard via sidecar (ConfigMap com label `grafana_dashboard=1`). Painéis **operacionais** consultam o datasource Prometheus existente (uid `prometheus`); painéis de **negócio** consultam o datasource Postgres read-only `nossagrana` (uid `nossagrana-pg`, banco real `nossagrana_prod`), criado na **Fase 0** (dependência — ver nota abaixo).

**Tech Stack:** Node 20 + Fastify 5 + TypeScript (ESM, imports com sufixo `.js`), pnpm workspaces + Turborepo, Drizzle ORM + PostgreSQL, Vitest. Observabilidade: kube-prometheus-stack (Prometheus Operator + Grafana) no K3s; ServiceMonitor/PrometheusRule (`monitoring.coreos.com/v1`); dashboards JSON como código.

**Dependência (não bloqueante para a instrumentação):** Os painéis de **negócio** do dashboard dependem do **datasource Postgres read-only `nossagrana`** (usuário `grafana_ro`, uid `nossagrana-pg`, banco real `nossagrana_prod`) provisionado na **Fase 0** (repo `chat-api-monitoring`, `k8s/monitoring/values.yaml` → `grafana.additionalDataSources`). As Tasks 1–4 (instrumentação) e a Task 5 (ServiceMonitor, painéis operacionais) **não dependem** da Fase 0 e podem ser executadas já. Os painéis de negócio (Task 6) usam `"uid": "nossagrana-pg"`; se a Fase 0 ainda não rodou, eles ficarão "No data" até o datasource existir — isso é esperado e não quebra o dashboard.

**Regra global:** Todo o trabalho roda em **git worktree** separada de cada repo (workspace principal limpo). Cada repo tem sua worktree.

**Skills do projeto nossagrana a respeitar:** `code-quality-guard` (autoApply — schema Fastify = contrato TS, sem `any`, sem `!`), `tdd-workflow` (Red→Green→Refactor), `pre-commit` (pipeline obrigatório de 9 etapas antes de cada commit: `pnpm format:check:changed`, `pnpm lint:fast`, `pnpm lint`, `pnpm type-check`, `pnpm build`, `pnpm knip`, `pnpm --filter api test -- --run`, testes web, coverage). Cobertura mínima 80% nas linhas dos arquivos alterados.

---

## Task 1 — Worktrees e branches nos dois repos

**Files:**
- (nenhum arquivo de código — só setup de git)

**Steps:**
- [ ] Criar worktree no repo nossagrana:
  ```bash
  git -C /home/leoferolive/projetos/nossagrana worktree add \
    ../nossagrana-metrics -b feat/api-metrics
  ```
  Output esperado: `Preparing worktree (new branch 'feat/api-metrics')` + `HEAD is now at <sha> ...`
- [ ] Criar worktree no repo chat-api-monitoring:
  ```bash
  git -C /home/leoferolive/projetos/chat-api-monitoring worktree add \
    ../chat-api-monitoring-nossagrana -b feat/nossagrana-dashboard
  ```
  Output esperado: `Preparing worktree (new branch 'feat/nossagrana-dashboard')`
- [ ] Confirmar worktrees: `git -C /home/leoferolive/projetos/nossagrana worktree list` deve listar `../nossagrana-metrics`.
- [ ] **A partir daqui, todos os caminhos do repo nossagrana referem-se à worktree** `/home/leoferolive/projetos/nossagrana-metrics/` e os do monitoring a `/home/leoferolive/projetos/chat-api-monitoring-nossagrana/`. (Os caminhos abaixo usam o repo canônico por clareza; ajuste para a worktree ao editar.)

---

## Task 2 — Adicionar dependência `fastify-metrics` ao apps/api

**Files:**
- Modify: `/home/leoferolive/projetos/nossagrana-metrics/apps/api/package.json`

**Steps:**
- [ ] Na worktree nossagrana, instalar `fastify-metrics` (compatível com Fastify 5; embute `prom-client`):
  ```bash
  pnpm --filter api add fastify-metrics
  ```
  Output esperado: `+ fastify-metrics <versão>` e `Done`. Confere que `apps/api/package.json` → `dependencies` ganhou `"fastify-metrics"`.
- [ ] Confirmar que `prom-client` veio como dependência transitiva (não precisa adicionar explicitamente):
  ```bash
  pnpm --filter api list fastify-metrics
  ```
  Output esperado: árvore mostrando `fastify-metrics` e `prom-client` aninhado.
- [ ] **NÃO commitar ainda** — o plugin e o teste vêm nas próximas tasks (commit único ao final da instrumentação, após pre-commit).

---

## Task 3 — TDD: teste falhando para `GET /metrics`

**Files:**
- Create: `/home/leoferolive/projetos/nossagrana-metrics/apps/api/src/plugins/metrics.plugin.test.ts`

**Steps:**
- [ ] **(Red)** Criar o teste do endpoint `/metrics` (sem prefixo `/api` — `/metrics` fica na raiz, como o padrão Prometheus espera). O teste usa o padrão `buildApp()` + `app.inject` já presente no repo:
  ```typescript
  import { afterAll, beforeAll, describe, expect, it } from 'vitest';

  import { buildApp } from '../app.js';

  describe('GET /metrics (Prometheus)', () => {
    const app = buildApp();

    beforeAll(async () => {
      await app.ready();
    });

    afterAll(async () => {
      await app.close();
    });

    it('responde 200 com payload no formato Prometheus', async () => {
      const response = await app.inject({ method: 'GET', url: '/metrics' });

      expect(response.statusCode).toBe(200);
      expect(response.headers['content-type']).toMatch(/text\/plain/);
      // Métricas default do prom-client (process_*) e histograma de request HTTP.
      expect(response.body).toContain('process_cpu_user_seconds_total');
      expect(response.body).toMatch(/# TYPE .* histogram/);
    });

    it('contém o histograma de duração de request HTTP após uma chamada', async () => {
      // Gera tráfego para popular o histograma por rota/método/status.
      await app.inject({ method: 'GET', url: '/api/health' });

      const response = await app.inject({ method: 'GET', url: '/metrics' });
      expect(response.body).toContain('http_request_duration_seconds');
    });

    it('NÃO expõe labels de alta cardinalidade (familia_id / user_id)', async () => {
      await app.inject({ method: 'GET', url: '/api/health' });
      const response = await app.inject({ method: 'GET', url: '/metrics' });
      expect(response.body).not.toMatch(/familia_id=/);
      expect(response.body).not.toMatch(/user_id=/);
    });
  });
  ```
- [ ] **(Red — ver falhar)** Rodar só este arquivo:
  ```bash
  pnpm --filter api test -- --run src/plugins/metrics.plugin.test.ts
  ```
  Output esperado: **FAIL** — `/metrics` retorna 404 (`statusCode` 404 ≠ 200), porque o plugin ainda não existe. Confirmar que falhou pela razão certa (rota inexistente), não por erro de import.

---

## Task 4 — Implementar o plugin `/metrics` (mínimo para passar)

**Files:**
- Create: `/home/leoferolive/projetos/nossagrana-metrics/apps/api/src/plugins/metrics.plugin.ts`
- Modify: `/home/leoferolive/projetos/nossagrana-metrics/apps/api/src/app.ts`

**Steps:**
- [ ] **(Green)** Criar o plugin. `fastify-metrics` registra automaticamente o endpoint `/metrics`, coleta as métricas default do `prom-client` e instrumenta um histograma de duração de request com labels `method`, `route`, `status_code` (baixa cardinalidade — usa o **template da rota**, não a URL com IDs). Configuração explícita para garantir buckets sensatos e os labels corretos:
  ```typescript
  import fp from 'fastify-plugin';
  import metricsPlugin from 'fastify-metrics';

  /**
   * Expõe GET /metrics no formato Prometheus.
   * - Métricas default do prom-client (process_*, nodejs_*).
   * - Histograma http_request_duration_seconds por método/rota/status.
   * - Sem labels de alta cardinalidade: usa o template da rota (ex: /api/transacoes/:id),
   *   nunca familia_id/user_id. /metrics fica na raiz, sem prefixo /api.
   */
  export const metricsPlugin_ = fp(async (app) => {
    await app.register(metricsPlugin, {
      endpoint: '/metrics',
      routeMetrics: {
        enabled: true,
        // Agrupa por template de rota (definido pelo schema Fastify), evitando
        // explosão de séries por URLs com UUIDs.
        groupStatusCodes: true,
        registeredRoutesOnly: true,
        overrides: {
          histogram: {
            name: 'http_request_duration_seconds',
            help: 'Duração das requests HTTP em segundos',
            buckets: [0.01, 0.05, 0.1, 0.3, 0.5, 1, 2.5, 5, 10],
          },
        },
      },
    });
  });
  ```
  > Nota: `fastify-metrics` expõe `/metrics` por padrão como `text/plain; version=0.0.4` e não exige auth. Como o endpoint é registrado pelo plugin direto na instância raiz (sem o prefix `/api`), ele já fica **fora** do agrupamento de rotas `/api/*`.
- [ ] **(Green)** Registrar o plugin em `buildApp()` **antes** das rotas e garantir que está **fora do rate-limit**. No `app.ts`, dois ajustes:
  1. Adicionar `/metrics` ao `allowList` do rate-limit (junto com `/api/health`):
     ```typescript
     allowList: (req) => req.url === '/api/health' || req.url === '/metrics',
     ```
  2. Importar e registrar o plugin de métricas logo após os plugins de segurança e antes do `authPlugin`:
     ```typescript
     import { metricsPlugin_ } from './plugins/metrics.plugin.js';
     // ...
     app.register(metricsPlugin_);
     ```
  > `/metrics` não passa pelo `authPlugin` (que só protege rotas via `onRequest` nas rotas que o exigem) e, com o `allowList`, fica fora do rate-limit. Helmet aplica headers, o que é inofensivo para texto Prometheus.
- [ ] **(Green — ver passar)** Rodar o teste:
  ```bash
  pnpm --filter api test -- --run src/plugins/metrics.plugin.test.ts
  ```
  Output esperado: **PASS** — 3 testes verdes (`200`, content-type `text/plain`, histograma presente, sem labels sensíveis).
- [ ] **(Refactor)** Conferir `code-quality-guard`: sem `any`, sem `!`, sem `reply.code()` fora de schema (o plugin não declara rota manual). Rodar `pnpm --filter api lint:fast` e corrigir avisos.
- [ ] **(Pre-commit)** Rodar o pipeline completo da skill `pre-commit` na worktree nossagrana, parando no primeiro erro:
  ```bash
  pnpm format:check:changed
  pnpm lint:fast
  pnpm lint
  pnpm type-check
  pnpm build
  pnpm knip
  pnpm --filter api test -- --run
  ```
  > Se `knip` acusar `metricsPlugin_` como export não usado (falso positivo de plugin), confirmar que está importado em `app.ts`; se persistir, adicionar ao `knip.config.ts`. Output esperado de cada etapa: sem erros; testes todos verdes.
- [ ] **(Commit)** Commitar a instrumentação na worktree nossagrana:
  ```bash
  git -C /home/leoferolive/projetos/nossagrana-metrics add apps/api/package.json \
    pnpm-lock.yaml apps/api/src/plugins/metrics.plugin.ts \
    apps/api/src/plugins/metrics.plugin.test.ts apps/api/src/app.ts
  git -C /home/leoferolive/projetos/nossagrana-metrics commit -m "feat(api): expor GET /metrics (Prometheus) sem auth e fora do rate-limit"
  ```

---

## Task 5 — ServiceMonitor para o Service da API nossagrana

**Files:**
- Create: `/home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/servicemonitors/nossagrana.yaml`

**Steps:**
- [ ] Criar o ServiceMonitor replicando o padrão de `k8s/prod/servicemonitor.yaml` do chat-api. O Service alvo é `nossagrana-api` (namespace `nossagrana`, selector `app: nossagrana-api`, porta nomeada `http`). A label `release: kps` é **obrigatória** (o `serviceMonitorSelector` do kube-prometheus-stack só pega ServiceMonitors com ela):
  ```yaml
  apiVersion: monitoring.coreos.com/v1
  kind: ServiceMonitor
  metadata:
    name: nossagrana-api
    namespace: nossagrana
    labels:
      app: nossagrana-api
      # Required by kube-prometheus-stack's serviceMonitorSelector.
      release: kps
  spec:
    selector:
      matchLabels:
        app: nossagrana-api
    namespaceSelector:
      matchNames:
        - nossagrana
    endpoints:
      - port: http
        path: /metrics
        interval: 30s
        scrapeTimeout: 10s
  ```
  > O Service `nossagrana-api` já tem `ports[].name: http` (port 80 → targetPort 3000). O scrape vai para `http://<pod>:3000/metrics` via o endpoint nomeado `http`.
- [ ] Validar o manifesto (dry-run server-side contra o cluster K3s; CRDs do Prometheus Operator já instalados):
  ```bash
  kubectl apply --dry-run=server \
    -f /home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/servicemonitors/nossagrana.yaml
  ```
  Output esperado: `servicemonitor.monitoring.coreos.com/nossagrana-api created (server dry run)`. Se `--dry-run=server` falhar por contexto kubectl, usar `--dry-run=client --validate=false` como fallback e anotar.

---

## Task 6 — Dashboard `dashboards/nossagrana.json` (negócio + operacional)

**Files:**
- Create: `/home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/dashboards/nossagrana.json`

**Steps:**
- [ ] Criar o dashboard JSON replicando o formato de `dashboards/chat-api.json`. Painéis **operacionais** usam `"datasource": {"type": "prometheus", "uid": "prometheus"}`; painéis de **negócio** usam `"datasource": {"type": "postgres", "uid": "nossagrana-pg"}` com `rawSql`. Conteúdo completo:
  ```json
  {
    "annotations": {"list": []},
    "editable": true,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 0,
    "id": null,
    "links": [],
    "liveNow": false,
    "panels": [
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "short"}},
        "gridPos": {"h": 4, "w": 6, "x": 0, "y": 0},
        "id": 1,
        "options": {"colorMode": "value", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT count(*) AS \"Usuários\" FROM users;",
            "refId": "A"
          }
        ],
        "title": "Usuários totais",
        "type": "stat"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "short"}},
        "gridPos": {"h": 4, "w": 6, "x": 6, "y": 0},
        "id": 2,
        "options": {"colorMode": "value", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT count(*) AS \"Famílias\" FROM familias WHERE deleted_at IS NULL;",
            "refId": "A"
          }
        ],
        "title": "Famílias ativas",
        "type": "stat"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "yellow", "value": null}, {"color": "green", "value": 1}]}}},
        "gridPos": {"h": 4, "w": 6, "x": 12, "y": 0},
        "id": 3,
        "options": {"colorMode": "value", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT count(DISTINCT usuario_registrou_id) AS \"Ativos 30d\" FROM transacoes WHERE criado_em >= now() - interval '30 days';",
            "refId": "A"
          }
        ],
        "title": "Usuários ativos (30d)",
        "type": "stat"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "short"}},
        "gridPos": {"h": 4, "w": 6, "x": 18, "y": 0},
        "id": 4,
        "options": {"colorMode": "value", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT count(*) AS \"Transações no mês\" FROM transacoes WHERE mes_referencia = to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM');",
            "refId": "A"
          }
        ],
        "title": "Transações no mês (contagem)",
        "type": "stat"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "currencyBRL", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 4},
        "id": 5,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "orientation": "horizontal"},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT coalesce(sum(valor) FILTER (WHERE tipo = 'receita'), 0) AS \"Receitas\", coalesce(sum(valor) FILTER (WHERE tipo = 'despesa'), 0) AS \"Despesas\", coalesce(sum(valor) FILTER (WHERE tipo = 'receita'), 0) - coalesce(sum(valor) FILTER (WHERE tipo = 'despesa'), 0) AS \"Saldo\" FROM transacoes WHERE mes_referencia = to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM');",
            "refId": "A"
          }
        ],
        "title": "Receitas vs Despesas vs Saldo (mês atual)",
        "type": "barchart"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "currencyBRL", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 4},
        "id": 6,
        "options": {"legend": {"displayMode": "list", "placement": "right"}, "pieType": "donut", "tooltip": {"mode": "single"}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT c.nome AS metric, sum(t.valor) AS value FROM transacoes t JOIN categorias c ON c.id = t.categoria_id WHERE t.tipo = 'despesa' AND t.mes_referencia = to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM') GROUP BY c.nome ORDER BY value DESC LIMIT 10;",
            "refId": "A"
          }
        ],
        "title": "Top categorias de despesa (mês atual)",
        "type": "piechart"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "short"}},
        "gridPos": {"h": 4, "w": 6, "x": 0, "y": 12},
        "id": 7,
        "options": {"colorMode": "value", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT count(*) AS \"Cofrinhos ativos\" FROM cofrinhos WHERE status = 'ativo';",
            "refId": "A"
          }
        ],
        "title": "Cofrinhos ativos",
        "type": "stat"
      },
      {
        "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
        "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}, {"color": "red", "value": 1}]}}},
        "gridPos": {"h": 4, "w": 6, "x": 6, "y": 12},
        "id": 8,
        "options": {"colorMode": "background", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}},
        "targets": [
          {
            "datasource": {"type": "postgres", "uid": "nossagrana-pg"},
            "format": "table",
            "rawQuery": true,
            "rawSql": "SELECT count(*) AS \"Orçamentos estourados\" FROM (SELECT o.id, o.valor_limite, coalesce(sum(t.valor), 0) AS gasto FROM orcamento_categoria o LEFT JOIN transacoes t ON t.categoria_id = o.categoria_id AND t.familia_id = o.familia_id AND t.tipo = 'despesa' AND t.mes_referencia = to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM') WHERE o.vigencia_inicio <= to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM') AND (o.vigencia_fim IS NULL OR o.vigencia_fim >= to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM')) GROUP BY o.id, o.valor_limite) sub WHERE sub.gasto > sub.valor_limite;",
            "refId": "A"
          }
        ],
        "title": "Orçamentos estourados (>100% no mês)",
        "type": "stat"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "reqps", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
        "id": 9,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "expr": "sum by (route, method) (rate(http_request_duration_seconds_count{namespace=\"nossagrana\"}[5m]))",
            "legendFormat": "{{method}} {{route}}",
            "refId": "A"
          }
        ],
        "title": "Req/s por rota (operacional)",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "s", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
        "id": 10,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "expr": "histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{namespace=\"nossagrana\"}[5m])))",
            "legendFormat": "p95 {{route}}",
            "refId": "A"
          }
        ],
        "title": "Latência p95 por rota (operacional)",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "reqps", "color": {"mode": "palette-classic"}, "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}, {"color": "red", "value": 0.1}]}}},
        "gridPos": {"h": 8, "w": 8, "x": 0, "y": 24},
        "id": 11,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "expr": "sum(rate(http_request_duration_seconds_count{namespace=\"nossagrana\",status_code=~\"5..\"}[5m]))",
            "legendFormat": "5xx/s",
            "refId": "A"
          }
        ],
        "title": "Taxa de erro 5xx (operacional)",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 8, "x": 8, "y": 24},
        "id": 12,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "expr": "sum by (pod) (rate(container_cpu_usage_seconds_total{namespace=\"nossagrana\",container=\"nossagrana-api\"}[5m]))",
            "legendFormat": "{{pod}}",
            "refId": "A"
          }
        ],
        "title": "CPU (cores) — nossagrana-api",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "bytes", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": 24},
        "id": 13,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "expr": "sum by (pod) (container_memory_working_set_bytes{namespace=\"nossagrana\",container=\"nossagrana-api\"})",
            "legendFormat": "{{pod}}",
            "refId": "A"
          }
        ],
        "title": "Memória — nossagrana-api",
        "type": "timeseries"
      }
    ],
    "refresh": "1m",
    "schemaVersion": 39,
    "tags": ["nossagrana"],
    "templating": {"list": []},
    "time": {"from": "now-6h", "to": "now"},
    "timepicker": {},
    "timezone": "America/Sao_Paulo",
    "title": "nossagrana",
    "uid": "nossagrana",
    "version": 1,
    "weekStart": ""
  }
  ```
  > Notas de aterramento no `schema.ts`: `familias.deletedAt` (`deleted_at`) → famílias ativas excluem soft-deleted; `transacoes.mesReferencia` (`mes_referencia`, formato `YYYY-MM`) é o campo correto para "mês atual" (e não `data`, por causa da regra de mês de referência de cartão); `transacoes.valor` é `numeric`; `categorias.tipo`/`transacoes.tipo` enum `receita|despesa`; `cofrinhos.status` enum `ativo|encerrado`; `orcamento_categoria` tem `valor_limite`, `vigencia_inicio`/`vigencia_fim` (texto `YYYY-MM`). O refresh é `1m` (Pi com recursos limitados — evita martelar o Postgres).
- [ ] Validar JSON bem-formado e que os UIDs de datasource estão corretos:
  ```bash
  jq -e '.uid == "nossagrana" and (.panels | length) == 13' \
    /home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/dashboards/nossagrana.json
  jq -e '[.panels[].datasource.uid] | unique | sort == ["nossagrana-pg","prometheus"]' \
    /home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/dashboards/nossagrana.json
  ```
  Output esperado: `true` em ambos (JSON válido, 13 painéis, só os dois datasources esperados). Note que o `uid` próprio do dashboard permanece `nossagrana` (`.uid == "nossagrana"`) — só os datasources de negócio usam `nossagrana-pg`.
  > **Confirmar o `type` do datasource contra a Fase 0:** o campo `"type"` dos painéis de negócio está como `postgres` (alias clássico). O datasource provisionado na Fase 0 (`grafana.additionalDataSources` em `values.yaml`) pode declarar o plugin como `grafana-postgresql-datasource` em versões recentes do Grafana. A **ligação real é por `uid` (`nossagrana-pg`)**, então o painel renderiza mesmo se o `type` divergir; ainda assim, alinhar o `"type"` ao valor exato do `values.yaml` da Fase 0 evita avisos no provisionamento. Conferir com `grep -A3 "uid: nossagrana-pg" k8s/monitoring/values.yaml` (no repo monitoring) e ajustar o `"type"` se necessário.

---

## Task 7 — (Opcional) PrometheusRule com 2 alertas

**Files:**
- Create: `/home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/prometheusrules/nossagrana.yaml`

**Steps:**
- [ ] Criar a PrometheusRule replicando o padrão de `k8s/prod/prometheusrule.yaml` (label `release: kps` obrigatória), com 2 alertas: erro 5xx alto e app down. Roteamento ao Telegram já é herdado do AlertManager existente:
  ```yaml
  apiVersion: monitoring.coreos.com/v1
  kind: PrometheusRule
  metadata:
    name: nossagrana-api
    namespace: nossagrana
    labels:
      app: nossagrana-api
      release: kps
  spec:
    groups:
      - name: nossagrana.health
        interval: 30s
        rules:
          - alert: NossagranaHighErrorRate
            expr: |
              sum(rate(http_request_duration_seconds_count{namespace="nossagrana",status_code=~"5.."}[5m]))
                /
              sum(rate(http_request_duration_seconds_count{namespace="nossagrana"}[5m])) > 0.05
            for: 5m
            labels:
              severity: warning
              namespace: nossagrana
            annotations:
              summary: "nossagrana-api: > 5% de erros 5xx"
              description: "Taxa de erro 5xx: {{ $value | humanizePercentage }} nos últimos 5min."
          - alert: NossagranaApiPodNotReady
            # `on(namespace, pod)` evita many-to-many com pods homônimos em outros namespaces.
            expr: |
              kube_pod_status_ready{namespace="nossagrana",condition="true"} == 0
                and on(namespace, pod)
              kube_pod_labels{namespace="nossagrana",label_app="nossagrana-api"} == 1
            for: 5m
            labels:
              severity: critical
              namespace: nossagrana
            annotations:
              summary: "nossagrana-api: pod não-ready há 5min"
              description: "Pod {{ $labels.pod }} sem readiness — verificar logs e /api/health."
  ```
- [ ] Validar:
  ```bash
  kubectl apply --dry-run=server \
    -f /home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/prometheusrules/nossagrana.yaml
  ```
  Output esperado: `prometheusrule.monitoring.coreos.com/nossagrana-api created (server dry run)`.

---

## Task 8 — Documentar deploy no README do monitoring + commit

**Files:**
- Modify: `/home/leoferolive/projetos/chat-api-monitoring-nossagrana/k8s/monitoring/README.md`

**Steps:**
- [ ] Adicionar uma seção "Dashboard nossagrana" ao README, replicando o padrão do chat-api (ConfigMap + label `grafana_dashboard=1`; sidecar com `searchNamespace: ALL` detecta automaticamente). Conteúdo a inserir:
  ```bash
  # Importar o dashboard nossagrana (sidecar auto-detecta via label)
  kubectl create configmap nossagrana-dashboard \
    -n monitoring \
    --from-file=nossagrana.json=k8s/monitoring/dashboards/nossagrana.json \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label configmap nossagrana-dashboard \
    -n monitoring grafana_dashboard=1 --overwrite

  # Aplicar ServiceMonitor + PrometheusRule (namespace nossagrana)
  kubectl apply -f k8s/monitoring/servicemonitors/nossagrana.yaml
  kubectl apply -f k8s/monitoring/prometheusrules/nossagrana.yaml
  ```
  Incluir nota: **os painéis de negócio exigem o datasource Postgres read-only `nossagrana` (uid `nossagrana-pg`, banco real `nossagrana_prod`) provisionado na Fase 0** (`grafana.additionalDataSources` em `values.yaml`); sem ele, esses painéis mostram "No data".
- [ ] Commitar tudo na worktree do monitoring:
  ```bash
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossagrana add \
    k8s/monitoring/servicemonitors/nossagrana.yaml \
    k8s/monitoring/dashboards/nossagrana.json \
    k8s/monitoring/prometheusrules/nossagrana.yaml \
    k8s/monitoring/README.md
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossagrana commit \
    -m "feat(nossagrana): ServiceMonitor + dashboard + alertas de métricas"
  ```

---

## Task 9 — Verificação pós-deploy (após merge e deploy nos dois repos)

**Files:**
- (nenhum — verificação operacional)

**Steps:**
- [ ] Após o deploy da API nossagrana (CI/CD via GitHub Actions na branch `main`), confirmar que `/metrics` responde no pod:
  ```bash
  kubectl -n nossagrana exec deploy/nossagrana-api -- \
    wget -qO- http://localhost:3000/metrics | head -20
  ```
  Output esperado: linhas `# HELP process_cpu_user_seconds_total ...` e `http_request_duration_seconds_bucket{...}`.
- [ ] Confirmar que o target está `UP` no Prometheus (após aplicar o ServiceMonitor):
  ```bash
  kubectl -n monitoring exec sts/prometheus-kps-kube-prometheus-stack-prometheus-0 -c prometheus -- \
    wget -qO- 'http://localhost:9090/api/v1/targets?state=active' | grep -o 'nossagrana-api'
  ```
  Output esperado: ao menos uma ocorrência de `nossagrana-api`.
- [ ] Validar o dashboard visualmente no Grafana usando o CLI **`browser-use`** (regra global — não usar Playwright MCP): abrir `https://grafana.leoferolive.com.br/d/nossagrana`, conferir que os painéis **operacionais** renderizam séries e que os painéis de **negócio** mostram dados não-vazios (depende da Fase 0 estar feita). Tirar screenshot de evidência:
  ```bash
  browser-use open https://grafana.leoferolive.com.br/d/nossagrana
  browser-use state
  browser-use screenshot
  browser-use close
  ```
- [ ] Se algum painel de negócio estiver vazio mas o target estiver UP, suspeitar de grant faltando no `grafana_ro` (Fase 0) — não é falha desta fase.
- [ ] Finalizar: usar a skill `superpowers:finishing-a-development-branch` para decidir merge/PR de cada worktree e limpar as worktrees (`git worktree remove`).
