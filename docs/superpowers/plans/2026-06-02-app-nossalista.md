# Dashboard nossalista — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Entregar observabilidade da app **nossalista** (Java 25 / Spring Boot 4.0.3 + React, Postgres): expor `/actuator/prometheus` (HTTP + JVM) no backend e provisionar, no repo `chat-api-monitoring`, um ServiceMonitor + um dashboard Grafana `nossalista.json` com painéis operacionais (Prometheus) e de negócio (SQL no Postgres `nossalista`).

**Architecture:**
- **Operacional:** backend adiciona `micrometer-registry-prometheus`, habilita o endpoint `/actuator/prometheus` na **porta 8080** (mesma porta da app — não há management port separado). O `ServiceMonitor` (namespace `nossalista`, label `release: kps`) faz scrape via Service `nossalista` no path `/actuator/prometheus`. Painéis operacionais consultam o datasource Prometheus existente (uid `prometheus`).
- **Negócio:** Grafana consulta o Postgres de prod do nossalista por um datasource read-only (uid `nossalista-pg`) **provisionado na Fase 0** (dependência externa, não bloqueante para a instrumentação). Painéis de negócio usam SQL direto sobre as tabelas reais (`users`, `lists`, `list_types`, `list_items`, `activity_logs`).
- **Dashboard como código:** JSON em `k8s/monitoring/dashboards/nossalista.json`, empacotado em ConfigMap com label `grafana_dashboard=1` no namespace `monitoring`; sidecar do kube-prometheus-stack auto-detecta (mesmo padrão do `chat-api.json`).
- **Worktrees (regra global):** dois repos, duas worktrees. Backend (`nossalista`) e observabilidade (`chat-api-monitoring`) são commits independentes.

**Tech Stack:** Java 25, Spring Boot 4.0.3, Spring Boot Actuator (já no pom), Micrometer Prometheus registry, JUnit 5 + MockMvc + `@SpringBootTest`, H2 (perfil `test`), Maven (`./mvnw -Pstrict-quality verify` — Checkstyle/PMD/SpotBugs/JaCoCo 80% linha / 75% branch), kube-prometheus-stack (Prometheus Operator + Grafana), Grafana datasources `prometheus` e `nossalista-pg`, `kubectl`, `jq`.

---

## Pré-requisitos e suposições

- **Worktree backend:** a partir de `/home/leoferolive/projetos/nossalista` (repo git; HEAD atual destacado — criar branch).
- **Worktree monitoring:** a partir de `/home/leoferolive/projetos/chat-api-monitoring` (branch atual `grafana-postgres-backend`).
- **Dependência Fase 0 (NÃO bloqueante para Tasks 1–4):** datasource Grafana `nossalista-pg` (Postgres read-only) precisa existir para os painéis de negócio (Task 6) renderizarem dados. A instrumentação (Tasks 1–3) e o ServiceMonitor + painéis operacionais (Tasks 4–5) **não dependem** da Fase 0. Se a Fase 0 ainda não rodou, o dashboard pode ser commitado mesmo assim — os painéis de negócio ficam vazios até o datasource existir.
- **Porta do Actuator:** a app expõe tudo na **8080** (não há `management.server.port` separado no deployment; probes usam `/actuator/health/*` na 8080). O ServiceMonitor aponta para a porta da app.
- **Segurança:** `SecurityConfig` termina com `.anyRequest().permitAll()` e `/api/**` exige auth — `/actuator/prometheus` (fora de `/api`) já fica **público** sem mudança. Adicionamos mesmo assim um matcher explícito por clareza/defesa (Task 3, opcional dentro da Task).
- **Datasource uid de negócio:** assumido `nossalista-pg` (a confirmar contra o que a Fase 0 provisionar; ajustar o uid no JSON se diferente).

---

## Task 1 — Worktrees + dependência Micrometer (TDD: teste falhando primeiro)

**Files:**
- Modify: `/home/leoferolive/projetos/nossalista/backend/pom.xml`
- Test (novo): `/home/leoferolive/projetos/nossalista/backend/src/test/java/br/com/leoferolive/nossalista/observability/PrometheusEndpointTest.java`

**Steps:**

- [ ] Criar worktree do backend (regra global — workspace principal limpo):
  ```bash
  git -C /home/leoferolive/projetos/nossalista worktree add -b feat/observability-prometheus /home/leoferolive/projetos/nossalista-observability HEAD
  ```
  Output esperado: `Preparing worktree (new branch 'feat/observability-prometheus')` e `HEAD is now at ...`.

- [ ] Escrever o teste que **vai falhar** (endpoint ainda não exposto). Criar o arquivo `src/test/java/br/com/leoferolive/nossalista/observability/PrometheusEndpointTest.java`:
  ```java
  package br.com.leoferolive.nossalista.observability;

  import org.junit.jupiter.api.BeforeEach;
  import org.junit.jupiter.api.Test;
  import org.springframework.beans.factory.annotation.Autowired;
  import org.springframework.boot.test.context.SpringBootTest;
  import org.springframework.test.web.servlet.MockMvc;
  import org.springframework.test.web.servlet.setup.MockMvcBuilders;
  import org.springframework.web.context.WebApplicationContext;

  import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
  import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
  import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

  /**
   * Garante que /actuator/prometheus responde 200 com payload no formato
   * de exposição Prometheus (text/plain), incluindo métricas HTTP e JVM.
   */
  @SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.MOCK)
  class PrometheusEndpointTest {

      @Autowired
      private WebApplicationContext webApplicationContext;

      private MockMvc mockMvc;

      @BeforeEach
      void setup() {
          mockMvc = MockMvcBuilders.webAppContextSetup(webApplicationContext).build();
      }

      @Test
      void prometheusEndpointShouldReturnOkWithPrometheusPayload() throws Exception {
          mockMvc.perform(get("/actuator/prometheus"))
                  .andExpect(status().isOk())
                  .andExpect(content().contentTypeCompatibleWith("text/plain"))
                  // Métrica padrão de JVM exposta pelo Micrometer
                  .andExpect(content().string(org.hamcrest.Matchers.containsString("jvm_memory_used_bytes")));
      }
  }
  ```

- [ ] Rodar só esse teste e **ver falhar** (endpoint 404, pois `prometheus` não está em `exposure.include` nem o registry está no classpath):
  ```bash
  /home/leoferolive/projetos/nossalista-observability/backend/mvnw -q -f /home/leoferolive/projetos/nossalista-observability/backend/pom.xml -Dtest=PrometheusEndpointTest test
  ```
  Output esperado: BUILD FAILURE com falha no teste (status 404 esperado 200, ou `NoClassDefFound`/contexto sem PrometheusMeterRegistry).

- [ ] Adicionar a dependência no `pom.xml`, logo após o bloco `spring-boot-starter-actuator` (linhas ~79-82):
  ```xml
          <!-- Micrometer Prometheus registry — expõe /actuator/prometheus -->
          <dependency>
              <groupId>io.micrometer</groupId>
              <artifactId>micrometer-registry-prometheus</artifactId>
          </dependency>
  ```
  (Sem `<version>` — gerenciado pelo `spring-boot-starter-parent` 4.0.3.)

- [ ] Confirmar que o Maven resolve a dependência:
  ```bash
  /home/leoferolive/projetos/nossalista-observability/backend/mvnw -q -f /home/leoferolive/projetos/nossalista-observability/backend/pom.xml dependency:get -Dartifact=io.micrometer:micrometer-registry-prometheus:LATEST -o 2>/dev/null; echo "ok-resolve"
  ```
  (Opcional/offline-tolerante — a validação real vem do build na Task 3.) Output esperado: sem erro fatal.

---

## Task 2 — Verificar gate de coverage (instrumentação não pode quebrar JaCoCo)

**Files:** (nenhuma alteração — etapa de verificação/raciocínio)

**Steps:**

- [ ] Confirmar que a mudança **não adiciona código de produção novo** (só dependência + properties), portanto o gate JaCoCo (`BUNDLE` LINE ≥ 0.80 / BRANCH ≥ 0.75 no `pom.xml`) não regride. O endpoint `/actuator/prometheus` é autoconfigurado pelo Spring Boot (sem classes nossas) e o teste novo o exercita.
- [ ] Anotar: se em algum momento for criada classe de configuração própria (não previsto neste plano), ela precisaria de teste. Aqui **não** criamos classe nova.

---

## Task 3 — Habilitar `/actuator/prometheus` nas properties (verde) + commit backend

**Files:**
- Modify: `/home/leoferolive/projetos/nossalista/backend/src/main/resources/application.yml`

**Steps:**

- [ ] Editar o bloco `management:` do `application.yml` (atualmente expõe `health,info`). Substituir por:
  ```yaml
  management:
    endpoints:
      web:
        exposure:
          include: health,info,prometheus
    endpoint:
      health:
        show-details: when-authorized
      prometheus:
        enabled: true
    prometheus:
      metrics:
        export:
          enabled: true
    metrics:
      tags:
        application: nossalista
  ```
  Notas: `prometheus` é incluído no exposure; `management.endpoint.prometheus.enabled=true` habilita o endpoint; `management.prometheus.metrics.export.enabled=true` é a chave do registry no Spring Boot 3+/4 (substitui a antiga `management.metrics.export.prometheus.enabled`); a tag `application=nossalista` rotula todas as séries.

- [ ] (Opcional, defesa-em-profundidade) Adicionar matcher explícito em `SecurityConfig.java` (linha 55), trocando:
  ```java
  .requestMatchers("/api/auth/**", "/api/health", "/actuator/health").permitAll()
  ```
  por:
  ```java
  .requestMatchers("/api/auth/**", "/api/health", "/actuator/health", "/actuator/prometheus").permitAll()
  ```
  (Já funciona via `anyRequest().permitAll()`, mas o matcher explícito documenta a intenção e sobrevive a futuras restrições do catch-all.)

- [ ] Rodar o teste de novo e **ver passar**:
  ```bash
  /home/leoferolive/projetos/nossalista-observability/backend/mvnw -q -f /home/leoferolive/projetos/nossalista-observability/backend/pom.xml -Dtest=PrometheusEndpointTest test
  ```
  Output esperado: BUILD SUCCESS, `Tests run: 1, Failures: 0`.

- [ ] Rodar o gate de qualidade completo (mesmo da CI — Checkstyle/PMD/SpotBugs/JaCoCo), pulando o dependency-check pesado (igual à CI que o roda em job separado):
  ```bash
  /home/leoferolive/projetos/nossalista-observability/backend/mvnw -B -f /home/leoferolive/projetos/nossalista-observability/backend/pom.xml -Pstrict-quality verify -Ddependency-check.skip=true
  ```
  Output esperado: BUILD SUCCESS, sem violações de Checkstyle/PMD/SpotBugs, JaCoCo dentro dos limites.

- [ ] Smoke local opcional (igual à CI) confirmando o payload Prometheus real:
  ```bash
  java -jar /home/leoferolive/projetos/nossalista-observability/backend/target/*.jar --spring.profiles.active=ci > /tmp/nl-smoke.log 2>&1 &
  APP_PID=$!; for i in $(seq 1 120); do curl -fsS http://127.0.0.1:8080/actuator/health >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS http://127.0.0.1:8080/actuator/prometheus | grep -E "http_server_requests_seconds|jvm_memory_used_bytes" | head
  kill "$APP_PID"
  ```
  Output esperado: linhas `# HELP/# TYPE` e amostras de `jvm_memory_used_bytes`; `http_server_requests_seconds_*` aparece após ≥1 request HTTP (o `curl /actuator/health` acima já gera tráfego).

- [ ] Atualizar documentação canônica impactada (regra do repo nossalista — `CLAUDE.md`). Acrescentar em `backend/QUALITY.md` ou `README.md` uma linha: "Métricas Prometheus expostas em `/actuator/prometheus` (Micrometer)". Verificar qual doc menciona Actuator:
  ```bash
  grep -rni "actuator" /home/leoferolive/projetos/nossalista-observability/README.md /home/leoferolive/projetos/nossalista-observability/backend/QUALITY.md
  ```
  Editar o arquivo encontrado adicionando a linha de métricas.

- [ ] Commit no repo backend:
  ```bash
  git -C /home/leoferolive/projetos/nossalista-observability add backend/pom.xml backend/src/main/resources/application.yml backend/src/main/java/br/com/leoferolive/nossalista/config/SecurityConfig.java backend/src/test/java/br/com/leoferolive/nossalista/observability/PrometheusEndpointTest.java
  git -C /home/leoferolive/projetos/nossalista-observability commit -m "feat(observability): expor /actuator/prometheus com micrometer"
  ```
  (Adicionar também o doc editado ao `git add` se aplicável. Sem co-author de IA — regra do repo.)

---

## Task 4 — Worktree monitoring + Service nomeado + ServiceMonitor

**Files:**
- Modify: `/home/leoferolive/projetos/nossalista/k8s/prod/service.yaml` (na worktree backend — adicionar `name: http` à porta)
- Create: `/home/leoferolive/projetos/chat-api-monitoring/k8s/nossalista/servicemonitor.yaml`

**Steps:**

- [ ] Criar worktree do repo monitoring:
  ```bash
  git -C /home/leoferolive/projetos/chat-api-monitoring worktree add -b feat/nossalista-dashboard /home/leoferolive/projetos/chat-api-monitoring-nossalista HEAD
  ```
  Output esperado: `Preparing worktree (new branch 'feat/nossalista-dashboard')`.

- [ ] **Nomear a porta do Service nossalista** (necessário para o ServiceMonitor referenciar `port: http`). O Service atual tem porta sem nome. Editar `k8s/prod/service.yaml` (na worktree do backend) de:
  ```yaml
  spec:
    selector:
      app: nossalista
    ports:
      - port: 80
        targetPort: 8080
    type: ClusterIP
  ```
  para:
  ```yaml
  spec:
    selector:
      app: nossalista
    ports:
      - name: http
        port: 80
        targetPort: 8080
    type: ClusterIP
  ```
  (Esse commit vai junto com o backend — pode ser amend no commit da Task 3 ou um commit separado `chore(k8s): nomear porta http do Service nossalista`. Sem nome de porta, o ServiceMonitor teria que usar `targetPort: 8080`, que é mais frágil.)

- [ ] Criar `k8s/nossalista/servicemonitor.yaml` (na worktree monitoring). O scrape ocorre na porta da app (8080 via Service port `http`), path `/actuator/prometheus`:
  ```yaml
  apiVersion: monitoring.coreos.com/v1
  kind: ServiceMonitor
  metadata:
    name: nossalista
    namespace: nossalista
    labels:
      app: nossalista
      # Exigido pelo serviceMonitorSelector do kube-prometheus-stack (release=kps).
      release: kps
  spec:
    selector:
      matchLabels:
        app: nossalista
    namespaceSelector:
      matchNames:
        - nossalista
    endpoints:
      - port: http
        path: /actuator/prometheus
        interval: 30s
        scrapeTimeout: 10s
  ```
  Notas: `selector.matchLabels.app=nossalista` casa com os labels do Service (`metadata.labels` ausente hoje, mas o **selector do ServiceMonitor casa contra os labels do Service**, não do pod — adicionar `labels: {app: nossalista}` ao Service se ele não os tiver). Ver passo seguinte.

- [ ] **Garantir labels no Service** para o selector do ServiceMonitor casar. O Service nossalista hoje só tem `name`/`namespace` em `metadata`. Adicionar labels no `k8s/prod/service.yaml` (worktree backend):
  ```yaml
  metadata:
    name: nossalista
    namespace: nossalista
    labels:
      app: nossalista
  ```
  (Sem isso o ServiceMonitor não encontra o Service.)

- [ ] Validar o YAML do ServiceMonitor localmente com `kubectl --dry-run` (cliente, sem cluster) e/ou contra o cluster se disponível:
  ```bash
  kubectl apply --dry-run=client -f /home/leoferolive/projetos/chat-api-monitoring-nossalista/k8s/nossalista/servicemonitor.yaml
  ```
  Output esperado: `servicemonitor.monitoring.coreos.com/nossalista created (dry run)`. (Se o CRD `ServiceMonitor` não estiver no kubeconfig local, usar `--validate=false` ou validar via `--dry-run=client -o yaml`.)

- [ ] Commit no repo monitoring (ServiceMonitor):
  ```bash
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossalista add k8s/nossalista/servicemonitor.yaml
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossalista commit -m "feat(nossalista): ServiceMonitor scrape /actuator/prometheus"
  ```

---

## Task 5 — Dashboard `nossalista.json`: painéis operacionais (Prometheus)

**Files:**
- Create: `/home/leoferolive/projetos/chat-api-monitoring/k8s/monitoring/dashboards/nossalista.json`

**Steps:**

- [ ] Criar o JSON com a **estrutura base + painéis operacionais primeiro** (datasource `prometheus`). Métricas-fonte: `http_server_requests_seconds_*` (Micrometer) e `kube-state-metrics`/cAdvisor para CPU/mem/uptime. Conteúdo inicial (painéis de negócio entram na Task 6 — manter o array `panels` aberto até lá, ou criar completo agora e validar no fim):

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
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "reqps", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
        "id": 1,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "expr": "sum by (uri, method) (rate(http_server_requests_seconds_count{namespace=\"nossalista\"}[5m]))",
            "legendFormat": "{{method}} {{uri}}",
            "refId": "A"
          }
        ],
        "title": "Req/s por rota",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "s", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
        "id": 2,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "expr": "histogram_quantile(0.95, sum by (le, uri) (rate(http_server_requests_seconds_bucket{namespace=\"nossalista\"}[5m])))",
            "legendFormat": "p95 {{uri}}",
            "refId": "A"
          }
        ],
        "title": "Latência p95 por rota",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "percentunit", "color": {"mode": "palette-classic"}, "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": null}, {"color": "yellow", "value": 0.01}, {"color": "red", "value": 0.05}]}}},
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
        "id": 3,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "expr": "sum(rate(http_server_requests_seconds_count{namespace=\"nossalista\",status=~\"5..\"}[5m])) / clamp_min(sum(rate(http_server_requests_seconds_count{namespace=\"nossalista\"}[5m])), 0.001)",
            "legendFormat": "erro 5xx",
            "refId": "A"
          }
        ],
        "title": "Taxa de erro 5xx",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "bytes", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
        "id": 4,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "expr": "sum by (area) (jvm_memory_used_bytes{namespace=\"nossalista\"})",
            "legendFormat": "heap+nonheap {{area}}",
            "refId": "A"
          },
          {
            "expr": "sum(jvm_memory_max_bytes{namespace=\"nossalista\",area=\"heap\"})",
            "legendFormat": "heap max",
            "refId": "B"
          }
        ],
        "title": "JVM memória (heap/nonheap)",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "palette-classic"}}},
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
        "id": 5,
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
          {
            "expr": "sum by (pod) (rate(container_cpu_usage_seconds_total{namespace=\"nossalista\",container=\"nossalista\"}[5m]))",
            "legendFormat": "{{pod}}",
            "refId": "A"
          }
        ],
        "title": "CPU (cores)",
        "type": "timeseries"
      },
      {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {"defaults": {"unit": "s", "color": {"mode": "fixed", "fixedColor": "green"}}},
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
        "id": 6,
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"},
        "targets": [
          {
            "expr": "max(process_uptime_seconds{namespace=\"nossalista\"})",
            "refId": "A"
          }
        ],
        "title": "Uptime do processo (JVM)",
        "type": "stat"
      }
    ],
    "refresh": "30s",
    "schemaVersion": 39,
    "tags": ["nossalista"],
    "templating": {"list": []},
    "time": {"from": "now-6h", "to": "now"},
    "timepicker": {},
    "timezone": "America/Sao_Paulo",
    "title": "nossalista",
    "uid": "nossalista",
    "version": 1,
    "weekStart": ""
  }
  ```
  Notas: namespace fixo `nossalista` (single-tenant, sem variável `$namespace` por enquanto — diferente do chat-api que usa multi-namespace). `container="nossalista"` casa com o nome do container no deployment.

- [ ] Validar JSON com `jq`:
  ```bash
  jq -e '.uid=="nossalista" and (.panels|length)>=6' /home/leoferolive/projetos/chat-api-monitoring-nossalista/k8s/monitoring/dashboards/nossalista.json
  ```
  Output esperado: `true`.

- [ ] **NÃO commitar ainda** — a Task 6 adiciona os painéis de negócio ao mesmo array `panels`. Commitar tudo junto no fim da Task 6.

---

## Task 6 — Dashboard `nossalista.json`: painéis de negócio (SQL Postgres) + commit

**Files:**
- Modify: `/home/leoferolive/projetos/chat-api-monitoring/k8s/monitoring/dashboards/nossalista.json`

**SQLs reais (baseados nas migrations V1–V9):**
- `users(id, created_at, onboarding_completed_at, ...)`
- `lists(id, type_id, owner_id, created_at)`, `list_types(id, name, slug)`
- `list_items(id, list_id, checked, created_at)`
- `activity_logs(id, user_id, action, created_at)`

**Steps:**

- [ ] Adicionar ao array `panels` os painéis de negócio abaixo (datasource `{"type":"grafana-postgresql-datasource","uid":"nossalista-pg"}` — ajustar o `type`/`uid` ao que a Fase 0 provisionar). Continuar a grade abaixo dos operacionais (`y` a partir de 24). Painéis:

  1) **Usuários totais** (stat):
  ```sql
  SELECT count(*) AS "Usuários totais" FROM users;
  ```

  2) **Usuários novos 7d / 30d** (stat, 2 alvos):
  ```sql
  -- novos 7d
  SELECT count(*) AS novos_7d FROM users WHERE created_at >= now() - interval '7 days';
  -- novos 30d (refId B)
  SELECT count(*) AS novos_30d FROM users WHERE created_at >= now() - interval '30 days';
  ```

  3) **DAU / WAU via activity_logs** (stat, 2 alvos):
  ```sql
  -- DAU (usuários distintos com atividade nas últimas 24h)
  SELECT count(DISTINCT user_id) AS dau FROM activity_logs WHERE created_at >= now() - interval '1 day';
  -- WAU (refId B)
  SELECT count(DISTINCT user_id) AS wau FROM activity_logs WHERE created_at >= now() - interval '7 days';
  ```

  4) **Listas por tipo** (piechart/barchart):
  ```sql
  SELECT lt.name AS metric, count(l.id) AS value
  FROM list_types lt
  LEFT JOIN lists l ON l.type_id = lt.id
  GROUP BY lt.name
  ORDER BY value DESC;
  ```

  5) **Itens totais e taxa de conclusão (% checked)** (stat, 2 alvos):
  ```sql
  -- itens totais
  SELECT count(*) AS itens_totais FROM list_items;
  -- taxa de conclusão (refId B) — 0..1, usar unit percentunit
  SELECT
    CASE WHEN count(*) = 0 THEN 0
         ELSE count(*) FILTER (WHERE checked) :: numeric / count(*)
    END AS taxa_conclusao
  FROM list_items;
  ```

  6) **Ações por dia (timeline de activity_logs)** (timeseries — usar a coluna de tempo como `time`):
  ```sql
  SELECT
    date_trunc('day', created_at) AS "time",
    count(*) AS "ações/dia"
  FROM activity_logs
  WHERE created_at >= now() - interval '30 days'
  GROUP BY 1
  ORDER BY 1;
  ```

  7) **Taxa de onboarding (% onboarding_completed_at preenchido)** (gauge, unit percentunit 0..1):
  ```sql
  SELECT
    CASE WHEN count(*) = 0 THEN 0
         ELSE count(*) FILTER (WHERE onboarding_completed_at IS NOT NULL) :: numeric / count(*)
    END AS taxa_onboarding
  FROM users;
  ```

- [ ] Cada painel de negócio segue este esqueleto (exemplo do painel "Usuários totais"; replicar variando `id`, `gridPos`, `title`, `rawSql`, `unit`, `type`):
  ```json
  {
    "datasource": {"type": "grafana-postgresql-datasource", "uid": "nossalista-pg"},
    "fieldConfig": {"defaults": {"unit": "short"}},
    "gridPos": {"h": 6, "w": 6, "x": 0, "y": 24},
    "id": 10,
    "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"},
    "targets": [
      {
        "datasource": {"type": "grafana-postgresql-datasource", "uid": "nossalista-pg"},
        "format": "table",
        "rawQuery": true,
        "rawSql": "SELECT count(*) AS \"Usuários totais\" FROM users;",
        "refId": "A"
      }
    ],
    "title": "Usuários totais",
    "type": "stat"
  }
  ```
  Para o painel de timeseries (ações/dia) usar `"format": "time_series"` e garantir a coluna alias `time`. Para "Listas por tipo" usar `"format": "table"` com colunas `metric`/`value` e `type: "piechart"`.

- [ ] Validar o JSON final com `jq` (sintaxe + nº de painéis: 6 operacionais + 7 de negócio = 13):
  ```bash
  jq -e '.uid=="nossalista" and (.panels|length)>=13 and (any(.panels[]; .datasource.uid=="prometheus")) and (any(.panels[]; .datasource.uid=="nossalista-pg"))' \
    /home/leoferolive/projetos/chat-api-monitoring-nossalista/k8s/monitoring/dashboards/nossalista.json
  ```
  Output esperado: `true`.

- [ ] Atualizar `k8s/monitoring/README.md` (seção de import de dashboard) acrescentando o bloco do nossalista, espelhando o do chat-api:
  ```bash
  # (adicionar ao README, na seção de dashboards)
  # kubectl create configmap nossalista-dashboard -n monitoring \
  #   --from-file=nossalista.json=k8s/monitoring/dashboards/nossalista.json \
  #   --dry-run=client -o yaml | kubectl apply -f -
  # kubectl label configmap nossalista-dashboard -n monitoring grafana_dashboard=1 --overwrite
  ```
  Editar `k8s/monitoring/README.md` adicionando essas instruções como bloco shell real.

- [ ] Commit no repo monitoring (dashboard + README):
  ```bash
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossalista add k8s/monitoring/dashboards/nossalista.json k8s/monitoring/README.md
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossalista commit -m "feat(nossalista): dashboard Grafana (operacional + negócio)"
  ```

---

## Task 7 — (Opcional) PrometheusRule: erro 5xx alto + app down

**Files:**
- Create: `/home/leoferolive/projetos/chat-api-monitoring/k8s/nossalista/prometheusrule.yaml`

**Steps:**

- [ ] Criar `k8s/nossalista/prometheusrule.yaml` espelhando o padrão do chat-api (label `release: kps`, namespace `nossalista`):
  ```yaml
  apiVersion: monitoring.coreos.com/v1
  kind: PrometheusRule
  metadata:
    name: nossalista
    namespace: nossalista
    labels:
      app: nossalista
      release: kps
  spec:
    groups:
      - name: nossalista.health
        interval: 30s
        rules:
          - alert: NossalistaHighErrorRate
            expr: |
              sum(rate(http_server_requests_seconds_count{namespace="nossalista",status=~"5.."}[5m]))
                /
              clamp_min(sum(rate(http_server_requests_seconds_count{namespace="nossalista"}[5m])), 0.001) > 0.05
            for: 5m
            labels:
              severity: warning
              namespace: nossalista
            annotations:
              summary: "nossalista: > 5% de erros 5xx"
              description: "Taxa de erro: {{ $value | humanizePercentage }} nos últimos 5min."

          - alert: NossalistaPodNotReady
            expr: |
              kube_pod_status_ready{namespace="nossalista",condition="true"} == 0
                and on(namespace, pod)
              kube_pod_labels{namespace="nossalista",label_app="nossalista"} == 1
            for: 5m
            labels:
              severity: critical
              namespace: nossalista
            annotations:
              summary: "nossalista: pod não-ready há 5min"
              description: "Pod {{ $labels.pod }} sem readiness — verificar logs e /actuator/health."

          - alert: NossalistaTargetDown
            expr: up{namespace="nossalista",job=~".*nossalista.*"} == 0
            for: 5m
            labels:
              severity: critical
              namespace: nossalista
            annotations:
              summary: "nossalista: target de scrape down"
              description: "Prometheus não consegue raspar /actuator/prometheus há 5min."
  ```

- [ ] Validar:
  ```bash
  kubectl apply --dry-run=client -f /home/leoferolive/projetos/chat-api-monitoring-nossalista/k8s/nossalista/prometheusrule.yaml
  ```
  Output esperado: `prometheusrule.monitoring.coreos.com/nossalista created (dry run)`.

- [ ] Commit:
  ```bash
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossalista add k8s/nossalista/prometheusrule.yaml
  git -C /home/leoferolive/projetos/chat-api-monitoring-nossalista commit -m "feat(nossalista): PrometheusRule erro 5xx + app down"
  ```

---

## Task 8 — Verificação pós-deploy (após merge/deploy — não bloqueante para o plano)

**Files:** (nenhuma — verificação operacional)

**Steps:**

- [ ] Após deploy do backend (CI/CD do nossalista) e `kubectl apply` dos manifestos de monitoring, confirmar que o target está `UP` no Prometheus:
  ```bash
  kubectl -n nossalista get servicemonitor nossalista
  # No Prometheus UI: targets → namespace nossalista → endpoint /actuator/prometheus = UP
  ```
- [ ] Confirmar que o dashboard apareceu no Grafana (`grafana.leoferolive.com.br`) e que os **painéis operacionais renderizam dados** (Prometheus). 
- [ ] Confirmar que os **painéis de negócio renderizam dados não-vazios** (requer datasource `nossalista-pg` da Fase 0). Se vazios: verificar o uid do datasource no JSON e os grants do usuário read-only.
- [ ] Validação visual via CLI `browser-use` (regra global — **não** usar Playwright MCP): `browser-use open https://grafana.leoferolive.com.br/d/nossalista` → `browser-use state` → `browser-use screenshot` como evidência → `browser-use close`.

---

## Notas finais de integração

- **Ordem de commits/repos:** backend (Tasks 1–3, + Service na Task 4) num repo/worktree; monitoring (Tasks 4–7) noutro. Abrir 2 PRs separados.
- **Dependência Fase 0:** painéis de negócio (Task 6) só renderizam com o datasource `nossalista-pg` provisionado. O plano entrega o JSON pronto; ajustar `uid`/`type` do datasource se a Fase 0 usar nomes diferentes.
- **Porta do Actuator = porta da app (8080):** o ServiceMonitor usa o Service port `http` (→ targetPort 8080). Não há management port separado.
- **CI gates:** a única mudança de produção é dependência + properties (sem classe nova) → JaCoCo não regride. O teste novo cobre o endpoint. Checkstyle/PMD/SpotBugs não tocam YAML/properties.
