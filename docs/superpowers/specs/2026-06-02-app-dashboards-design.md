# Design — Dashboards de métricas por aplicação (nossalista, nossagrana, chat-api)

Data: 2026-06-02
Status: Aprovado (brainstorming) — pronto para plano de implementação

## Objetivo

Entregar um dashboard Grafana por aplicação (**nossalista**, **nossagrana**, **chat-api**)
com as métricas básicas mais usadas de cada uma, cobrindo tanto **negócio** quanto
**operacional**, reaproveitando a stack de observabilidade que já roda no cluster K3s.

## Decisões travadas no brainstorming

1. **Tipos de métrica:** negócio **e** operacional.
2. **Coleta de negócio:** Grafana consulta o PostgreSQL de cada app via **datasource
   PostgreSQL read-only** (SQL direto). Zero gauges de negócio nas apps Postgres.
   chat-api (SQLite) é exceção — negócio exposto via Prometheus (gauges polados do SQLite).
3. **Ambiente:** produção primeiro.
4. **Organização (abordagem A):** dashboards JSON, datasources e ServiceMonitors
   centralizados no repo `chat-api-monitoring` (já tem esse padrão). A instrumentação
   de código fica no repo de cada app.
5. **Execução:** as 3 apps em paralelo (repos independentes), cada uma em git worktree.

## Estado atual (levantado na exploração)

| App | Stack | Banco prod | Namespace prod | Métricas hoje |
|-----|-------|-----------|----------------|---------------|
| chat-api | Python/FastAPI | SQLite (`/data/chat.sqlite` no pod) | `chat-api` | ✅ Prometheus (11 métricas), dashboard 8 painéis, alertas |
| nossalista | Java/Spring Boot 4 + React | PostgreSQL | `nossalista` | ❌ Actuator no pom, sem registry/endpoint |
| nossagrana | Node/Fastify + React | PostgreSQL (`nossagrana-postgres`) | `nossagrana` | ❌ nenhuma |

Stack de monitoramento existente (`chat-api-monitoring`):
- kube-prometheus-stack (Prometheus Operator + Grafana) via Helm, values em
  `k8s/monitoring/values.yaml`.
- Grafana em `grafana.leoferolive.com.br`, backend Postgres
  (`postgres.database.svc.cluster.local:5432`), provisionamento via Helm values.
- Padrão de dashboard como código: `k8s/monitoring/dashboards/chat-api.json`.
- Padrão de scrape: `k8s/{dev,prod}/servicemonitor.yaml` (ServiceMonitor → `/metrics`, 30s).
- Alertas via `prometheusrule.yaml` roteados ao Telegram.

### Fatos de infraestrutura confirmados / a confirmar
- **nossagrana DB (confirmado):** Service `nossagrana-postgres.nossagrana.svc.cluster.local:5432`,
  database `nossagrana`, secret `nossagrana-postgres-secrets`.
- **nossalista DB (a confirmar na Fase 0):** conexão injetada via secret `nossalista-secrets`
  (envFrom) no `k8s/prod/deployment.yaml`; host/porta/db não estão em manifesto. Descobrir
  in-cluster via `kubectl -n nossalista get secret nossalista-secrets -o yaml` (decodificar
  a connection string). Provável Postgres compartilhado em `database` namespace ou dedicado.
- Acesso ao cluster: kubectl direto disponível (homelab K3s, Raspberry Pi).

## Arquitetura da solução

```
┌──────────────── cluster K3s ────────────────┐
│                                              │
│  Prometheus ◀─ ServiceMonitor(nossalista)    │  (operacional)
│      ▲      ◀─ ServiceMonitor(nossagrana)    │
│      │      ◀─ ServiceMonitor(chat-api) [já] │
│      │                                       │
│   Grafana ─── datasource Prometheus [já]     │
│      │    ─── datasource PG nossalista (ro)  │  (negócio via SQL)
│      │    ─── datasource PG nossagrana (ro)  │
│      ▼                                       │
│   3 dashboards (JSON provisionados)          │
└──────────────────────────────────────────────┘
```

- **Operacional:** cada app expõe `/metrics` (Prometheus). ServiceMonitor faz scrape.
  Painéis consultam o datasource Prometheus existente.
- **Negócio:** Grafana consulta o Postgres de cada app por um usuário **read-only**
  dedicado. Painéis usam SQL. chat-api mantém negócio no Prometheus (gauges do SQLite).

## Componentes e unidades de trabalho

### Fase 0 — Infra compartilhada (repo `chat-api-monitoring`)
1. Descobrir conexão do Postgres de prod do nossalista (kubectl).
2. Criar usuário PostgreSQL **read-only** em cada banco de prod:
   - `GRANT CONNECT`, `USAGE` no schema, `SELECT` nas tabelas relevantes; sem escrita.
   - Um usuário por banco (`grafana_ro`), senha em secret.
3. Provisionar **2 datasources PostgreSQL** no Grafana via Helm values
   (`additionalDataSources`), apontando para os Services in-cluster, TLS conforme suporte,
   credenciais via secret/`secureJsonData`.
4. Confirmar **como o Grafana provisiona dashboards** (sidecar/configmap vs Helm values) e
   em que formato espera os JSON — pré-requisito das Fases 1–3, que entregam `dashboards/*.json`.
   Padrão já existe em `k8s/monitoring/dashboards/chat-api.json`; formalizar o caminho.
5. Documentar criação dos usuários/secret em `k8s/monitoring/README.md`.

### Fase 1 — nossagrana (repos `nossagrana` + `chat-api-monitoring`)
Instrumentação (repo nossagrana):
- Adicionar `fastify-metrics` (ou `prom-client` + plugin) ao `apps/api`.
- Expor `GET /metrics` (formato Prometheus), métricas default + histograma de duração
  de request por rota/método/status.
- Garantir que `/metrics` não exige auth e está fora do rate-limit; não vazar dados
  sensíveis (sem labels de família/usuário em alta cardinalidade).
Observabilidade (repo monitoring):
- ServiceMonitor para o Service da API nossagrana.
- Dashboard `dashboards/nossagrana.json`.
- (Opcional) PrometheusRule com 1–2 alertas (erro 5xx alto, app down).

### Fase 2 — nossalista (repos `nossalista` + `chat-api-monitoring`)
Instrumentação (repo nossalista):
- Adicionar `micrometer-registry-prometheus`; habilitar
  `management.endpoints.web.exposure.include=prometheus,health` e
  `management.endpoint.prometheus.enabled=true`.
- Endpoint `/actuator/prometheus` exposto na porta do app (ou management port);
  expor métricas HTTP (`http_server_requests_seconds`) e JVM padrão.
Observabilidade (repo monitoring):
- ServiceMonitor apontando para `/actuator/prometheus`.
- Dashboard `dashboards/nossalista.json`.
- (Opcional) PrometheusRule (erro 5xx, app down).

### Fase 3 — chat-api (repos `chat-api` + `chat-api-monitoring`)
- Adicionar 3 gauges de negócio em `app/metrics.py`, atualizados por um job/timer que
  faz `COUNT` no SQLite (janela diária, baixa frequência p.ex. 60s):
  `chat_api_sessions_today`, `chat_api_unique_users_today`, `chat_api_messages_today`.
- Acrescentar uma linha de painéis de negócio ao `dashboards/chat-api.json` existente
  (não remover os 8 painéis atuais).

## Métricas por dashboard ("básicas mais usadas")

### nossagrana — Finanças familiares
Negócio (SQL no Postgres nossagrana):
- Usuários e famílias totais
- Usuários ativos (com transação recente, p.ex. 30d)
- Transações no mês (contagem + volume R$)
- Receitas vs despesas vs saldo (mês de referência atual)
- Top categorias de despesa
- Cofrinhos ativos / orçamentos estourados (>100% do limite)
Operacional (Prometheus):
- Req/s e latência p95 por rota
- Taxa de erro 5xx
- CPU/mem do pod + uptime

### nossalista — Listas compartilhadas
Negócio (SQL no Postgres nossalista):
- Usuários totais e novos (7d/30d)
- Usuários ativos DAU/WAU (via `activity_logs`)
- Listas por tipo (`list_types`)
- Itens totais e taxa de conclusão (% `checked`)
- Ações por dia (timeline de `activity_logs`)
- Taxa de onboarding (% `onboarding_completed_at` preenchido)
Operacional (Prometheus):
- Req/s e latência p95 por rota (`http_server_requests_seconds`)
- Taxa de erro 5xx
- CPU/mem JVM (heap) + uptime

### chat-api — Assistente LLM (complementar ao dashboard existente)
Negócio (Prometheus, novos gauges):
- Conversas (sessions) por dia
- Usuários únicos por dia
- Mensagens por dia
Operacional: manter os 8 painéis atuais (tokens, latência p50/p95/p99, provider mix,
falhas, cost gate, CPU, memória).

## Tratamento de erros e robustez
- Datasource Postgres read-only: queries com timeout; falha de datasource degrada só os
  painéis de negócio, não derruba o dashboard.
- `/metrics` das apps deve responder mesmo sob carga; histogramas com buckets sensatos
  para evitar explosão de séries.
- Cardinalidade: **não** usar IDs de usuário/família como label Prometheus. Agregações
  finas ficam no SQL (negócio), não no Prometheus.
- Read-only de verdade: o usuário do Grafana não pode ter `INSERT/UPDATE/DELETE`.

## Estratégia de testes / verificação
- nossagrana/nossalista: teste que `/metrics` (ou `/actuator/prometheus`) responde 200
  com payload Prometheus válido; build/lint/test do repo passam (skills pre-commit).
- Após deploy: confirmar no Prometheus que o target está `UP` (ServiceMonitor pegou).
- Validar cada dashboard visualmente no Grafana (`grafana.leoferolive.com.br`) usando o
  CLI `browser-use` (regra global) — screenshot de evidência por dashboard.
- Confirmar que o usuário `grafana_ro` não consegue escrever (teste negativo de `INSERT`).
- Cada painel de negócio deve **renderizar dados não-vazios** (não só validar sintaxe):
  um grant/query errado passaria no check de target UP mas mostraria painel vazio.

## Riscos e questões abertas
- **Host do Postgres do nossalista** (Fase 0, bloqueante para o datasource de negócio).
- Postgres do nossagrana pode estar com recursos limitados (Raspberry Pi) — queries de
  negócio devem ser leves/indexadas; usar intervalos de refresh moderados no Grafana.
- chat-api: COUNT em SQLite num timer precisa ser barato; reusar conexão async existente.
- Persistência de dashboards: confirmar se o Grafana provisiona via sidecar/configmap
  (dashboards como código) e em qual formato espera os JSON.

## Fora de escopo (YAGNI)
- Cohorts de retenção avançada, painéis de custo em USD por provider no chat-api.
- Alertas sofisticados além de "app down" / "erro 5xx alto" opcionais.
- Dashboards de dev (foco prod; dev pode reusar via variável depois).
- Métricas de negócio do nossalista/nossagrana via gauges no app (escolhido SQL direto).
