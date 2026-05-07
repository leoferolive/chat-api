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

## Instalação

```bash
# 1. Adicionar repo Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# 2. Criar namespace
kubectl apply -f k8s/monitoring/namespace.yaml

# 3. Criar Secrets (alertmanager-telegram + grafana-admin) — NÃO commitar
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
