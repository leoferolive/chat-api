# Stack de monitoramento — chat-api / k3s

Instala `kube-prometheus-stack` (Prometheus Operator + Prometheus + Grafana
+ AlertManager + node-exporter + kube-state-metrics) no namespace
`monitoring`, com alertas roteados para um bot do Telegram dedicado.

## Pré-requisitos

- `kubectl` apontando para o cluster k3s (`leo-ubuntu`).
- `helm` v3 instalado localmente:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  ```

- Um bot do Telegram criado via `@BotFather`. Guarde o `bot_token` e o
  `chat_id` do destino (use um chat dedicado a alertas).

  Para descobrir o `chat_id`: envie qualquer mensagem ao bot e abra
  `https://api.telegram.org/bot<TOKEN>/getUpdates` — o `chat.id` aparece
  no JSON. Use o ID **negativo** se for um grupo, positivo se for DM.

- Acesso ao Postgres já rodando no cluster (namespace `database`,
  Service `postgres.database.svc.cluster.local:5432`, credenciais root
  no Secret `postgres-secret`). O Grafana usa um DB e user dedicados
  criados a partir desse Postgres — ver "Provisionar Postgres do
  Grafana" abaixo.

## Provisionar Postgres do Grafana

Idempotente — re-rodar atualiza a senha sem erro. A senha é passada
para o `psql` via variável (`-v pw=`), evitando aparecer em `ps` no
host local e ficar inline no SQL.

```bash
GRAFANA_DB_PASS=$(openssl rand -hex 16)
PG_POD=$(kubectl get pods -n database --no-headers | head -1 | awk '{print $1}')

kubectl exec -n database "$PG_POD" -i -- \
  env PGPASSWORD=root \
  psql -U root -d root -v ON_ERROR_STOP=1 -v pw="$GRAFANA_DB_PASS" <<'EOF'
SELECT 'CREATE DATABASE grafana'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'grafana')\gexec

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana') THEN
    EXECUTE format('CREATE ROLE grafana LOGIN PASSWORD %L', :'pw');
  ELSE
    EXECUTE format('ALTER ROLE grafana WITH PASSWORD %L', :'pw');
  END IF;
END $$;

GRANT ALL PRIVILEGES ON DATABASE grafana TO grafana;
ALTER DATABASE grafana OWNER TO grafana;
EOF

kubectl exec -n database "$PG_POD" -- env PGPASSWORD=root \
  psql -U root -d grafana -c "GRANT ALL ON SCHEMA public TO grafana;"

# Secret consumido pelo Grafana via envFromSecret (key vira env var)
kubectl create secret generic grafana-postgres \
  -n monitoring \
  --from-literal=GF_DATABASE_PASSWORD="$GRAFANA_DB_PASS" \
  --dry-run=client -o yaml | kubectl apply -f -
```

O Grafana cria o schema automaticamente ao subir contra um DB vazio
(85+ tabelas; auto-migrations a cada upgrade do chart).

> **Dependência operacional:** o pod do Grafana agora tem hard-dependency
> em `postgres.database`. Se o Postgres cair, Grafana entra em CrashLoop.
> Mesma classe de criticidade dos outros DBs do cluster (`nossagrana_*`,
> `nossalista_*`, etc).

## Instalação

```bash
# 1. Adicionar repo Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# 2. Criar namespace
kubectl apply -f k8s/monitoring/namespace.yaml

# 3. Criar Secrets (alertmanager-telegram + grafana-admin + grafana-postgres)
#    — NÃO commitar. O grafana-postgres é provisionado no passo "Provisionar
#    Postgres do Grafana" acima.
kubectl create secret generic alertmanager-telegram \
  -n monitoring \
  --from-literal=bot_token='123456:ABC-DEF...'

kubectl create secret generic grafana-admin \
  -n monitoring \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(openssl rand -hex 16)"

# 4. Editar k8s/monitoring/values.yaml e substituir os dois `chat_id: 0`
#    pelo chat_id real obtido via BotFather/getUpdates.

# 5. Instalar o chart
helm install kps prometheus-community/kube-prometheus-stack \
  -n monitoring \
  -f k8s/monitoring/values.yaml \
  --version "*"

# 6. Importar o dashboard chat-api (sidecar auto-detecta via label)
kubectl create configmap chat-api-dashboard \
  -n monitoring \
  --from-file=chat-api.json=k8s/monitoring/dashboards/chat-api.json
kubectl label configmap chat-api-dashboard \
  -n monitoring \
  grafana_dashboard=1

# 7. Aplicar ServiceMonitor + PrometheusRule (dev primeiro, depois prod)
kubectl apply -f k8s/dev/servicemonitor.yaml -f k8s/dev/prometheusrule.yaml
kubectl apply -f k8s/prod/servicemonitor.yaml -f k8s/prod/prometheusrule.yaml
```

Para atualizar o dashboard depois de editá-lo:

```bash
kubectl create configmap chat-api-dashboard \
  -n monitoring \
  --from-file=chat-api.json=k8s/monitoring/dashboards/chat-api.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl label configmap chat-api-dashboard \
  -n monitoring grafana_dashboard=1 --overwrite
```

### Dashboard nossalista

Operacional via Prometheus (datasource `prometheus`) + negócio via Postgres
read-only (datasource `nossalista-pg`). O ServiceMonitor scrapeia
`/actuator/prometheus` (porta `http` do Service `nossalista`).

```bash
# Importar / atualizar o dashboard nossalista (sidecar auto-detecta via label)
kubectl create configmap nossalista-dashboard \
  -n monitoring \
  --from-file=nossalista.json=k8s/monitoring/dashboards/nossalista.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl label configmap nossalista-dashboard \
  -n monitoring grafana_dashboard=1 --overwrite

# Aplicar ServiceMonitor (+ PrometheusRule, se presente)
kubectl apply -f k8s/nossalista/servicemonitor.yaml
kubectl apply -f k8s/nossalista/prometheusrule.yaml
```

## Verificação

```bash
# Pods Running
kubectl get pods -n monitoring

# CRDs criados
kubectl get crd | grep monitoring.coreos.com

# Targets via port-forward (outra aba)
kubectl port-forward -n monitoring svc/kps-prometheus 9090:9090
# → http://localhost:9090/targets — chat-api/chat-api deve aparecer UP
```

Grafana fica em `http://grafana.leoferolive.com.br` (Ingress via Traefik).
Login com as credenciais do Secret `grafana-admin`.

## Alertas configurados

Definidos em `k8s/prod/prometheusrule.yaml` e `k8s/dev/prometheusrule.yaml`:

| Alerta                       | Severity   | Trigger                                      |
|------------------------------|------------|----------------------------------------------|
| `ChatApiHighDailyTokens`     | warning    | Soma de tokens nas últimas 24h > 500 000     |
| `ChatApiCriticalDailyTokens` | critical   | Soma de tokens nas últimas 24h > 1 000 000   |
| `ChatApiHighChatRate`        | warning    | > 30 chats/minuto sustentado por 5min        |
| `ChatApiCostGateHit`         | warning    | DAILY_LLM_CALL_LIMIT atingido nos últimos 5min |
| `ChatApiHighErrorRate`       | warning    | > 5% de chats com erro nos últimos 5min      |
| `ChatApiAllProvidersFailing` | critical   | Toda chamada de LLM falhando há 5min         |
| `ChatApiHighLatency`         | warning    | p95 > 10s sustentado por 10min               |
| `ChatApiPodNotReady`         | critical   | Pod chat-api não-ready por 5min              |
| `ChatApiPodRestarting`       | warning    | > 3 restarts em 15min                        |

Thresholds são ajustáveis: edite `prometheusrule.yaml` e `kubectl apply`.

## Upgrade

```bash
helm upgrade kps prometheus-community/kube-prometheus-stack \
  -n monitoring -f k8s/monitoring/values.yaml
```

## Desinstalação

```bash
helm uninstall kps -n monitoring
kubectl delete pvc -n monitoring -l release=kps
kubectl delete crd \
  alertmanagerconfigs.monitoring.coreos.com \
  alertmanagers.monitoring.coreos.com \
  podmonitors.monitoring.coreos.com \
  probes.monitoring.coreos.com \
  prometheusagents.monitoring.coreos.com \
  prometheuses.monitoring.coreos.com \
  prometheusrules.monitoring.coreos.com \
  scrapeconfigs.monitoring.coreos.com \
  servicemonitors.monitoring.coreos.com \
  thanosrulers.monitoring.coreos.com
```
