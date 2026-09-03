# Production integrations

SentinelOps keeps the agent workflow independent of the infrastructure provider. Set `SENTINELOPS_INFRA_BACKEND` to select one implementation of the six-tool contract:

| Backend | Read path | Write path |
| --- | --- | --- |
| `simulator` | in-memory service state and logs | deterministic state mutations |
| `aws_ecs` | CloudWatch Logs, CloudWatch Metrics, ECS service state | ECS restart, desired-count scaling, task-definition rollback |
| `gcp_cloud_run` | Cloud Logging, Cloud Monitoring, Cloud Run service state | new-revision restart, minimum-instance scaling, revision traffic rollback |
| `kubernetes` | pod logs, metrics-server CPU, Deployment health/SLO annotations | rollout restart, scale subresource, ReplicaSet rollback |

`eks` and `gke` are aliases for `kubernetes`; authentication is supplied by the selected kubeconfig context or in-cluster service account. The Kubernetes API is the control path in both cases. Use CloudWatch or Cloud Monitoring exporters separately when centralized application SLO metrics are preferred.

## Installation

Install only the provider SDKs needed by the deployment:

```bash
python -m pip install -e '.[api,llm,aws]'
python -m pip install -e '.[api,llm,gcp]'
python -m pip install -e '.[api,llm,kubernetes]'
```

For a container containing every optional provider:

```bash
docker build --build-arg SENTINELOPS_EXTRAS=production -t sentinelops .
```

The SDKs use ambient workload credentials. Do not put long-lived AWS or GCP keys in `.env` or the image.

## AWS ECS with CloudWatch

Configure:

```env
SENTINELOPS_INFRA_BACKEND=aws_ecs
SENTINELOPS_AWS_REGION=us-east-1
SENTINELOPS_AWS_ECS_CLUSTER=production
SENTINELOPS_AWS_LOG_GROUP_TEMPLATE=/aws/ecs/{service}
SENTINELOPS_AWS_METRIC_NAMESPACE=SentinelOps/Services
```

The provider queries the last 15 minutes by default. Each protected service must emit these CloudWatch metrics with `ClusterName` and `ServiceName` dimensions:

| Metric | Unit expected by SentinelOps |
| --- | --- |
| `ErrorRate` | ratio from `0.0` to `1.0` |
| `P95LatencyMilliseconds` | milliseconds |
| `CPUUtilization` | percentage from `0` to `100` |

Missing SLO metrics fail closed: the diagnostic tool returns an error instead of treating missing data as healthy.

The workload role needs read access to the configured log groups and metrics plus `ecs:DescribeServices`. The execution identity additionally needs narrowly resource-scoped `ecs:UpdateService` and `ecs:ListTaskDefinitions`. For stronger isolation, deploy the remediator behind a separate authenticated service with the write role rather than sharing the write role with the API container.

Actions map as follows:

- `restart_service`: `UpdateService(forceNewDeployment=True)`
- `scale_service`: `UpdateService(desiredCount=N)`
- `rollback_deployment`: finds the preceding active task-definition revision and updates the service to it

## GCP Cloud Run with Cloud Logging and Monitoring

Configure:

```env
SENTINELOPS_INFRA_BACKEND=gcp_cloud_run
SENTINELOPS_GCP_PROJECT=my-project
SENTINELOPS_GCP_REGION=us-central1
SENTINELOPS_GCP_METRIC_ERROR_RATE=custom.googleapis.com/sentinelops/error_rate
SENTINELOPS_GCP_METRIC_P95_LATENCY_MS=custom.googleapis.com/sentinelops/p95_latency_ms
SENTINELOPS_GCP_METRIC_CPU_PERCENT=custom.googleapis.com/sentinelops/cpu_percent
```

The configured metrics must be written against a monitored resource containing `service_name`. Error rate is a `0.0`-to-`1.0` ratio, latency is milliseconds, and CPU is a percentage. Missing time series fail closed.

The logging adapter queries recent `cloud_run_revision` entries for the selected service. The monitoring adapter reads the configured time series, and the Cloud Run v2 API supplies revision and scaling state.

Actions map as follows:

- `restart_service`: updates a controlled restart timestamp environment variable, creating a new revision
- `scale_service`: updates the service's minimum instance count and raises the maximum when necessary
- `rollback_deployment`: directs 100% of traffic to the preceding revision

The read identity needs Logging Viewer, Monitoring Viewer, and Cloud Run Viewer-equivalent access. The execution identity additionally needs permission to update only the protected Cloud Run services.

## Kubernetes, EKS, and GKE

Configure:

```env
SENTINELOPS_INFRA_BACKEND=kubernetes
SENTINELOPS_K8S_NAMESPACE=production
SENTINELOPS_K8S_LABEL_KEY=app.kubernetes.io/name
SENTINELOPS_K8S_CONTEXT=
```

SentinelOps first tries in-cluster configuration, then the selected kubeconfig context. Service names map to Deployment names, and pods are selected with `<label key>=<service>`.

The Kubernetes metrics API does not provide application error rate or p95 latency. A monitoring controller or exporter must write the current SLO values to Deployment annotations:

```yaml
metadata:
  annotations:
    sentinelops.ai/error-rate: "0.012"
    sentinelops.ai/p95-latency-ms: "280"
```

Missing annotations fail closed. CPU comes from `metrics.k8s.io`; desired and available replicas come from Deployment status. Logs come from the pod log API.

Actions map as follows:

- `restart_service`: patches the pod-template restart timestamp
- `scale_service`: patches the Deployment scale subresource
- `rollback_deployment`: restores the pod template from the preceding owned ReplicaSet revision

Use namespace-scoped RBAC. The read role needs `get/list` for Deployments, ReplicaSets and pods, `get` for `pods/log`, and access to pod metrics. The write role needs only `patch` for Deployments and their scale subresource.

## Pub/Sub alert ingestion

`POST /api/events/pubsub` accepts a standard Pub/Sub push envelope. The base64-decoded message data must contain an incident payload:

```json
{
  "title": "checkout error budget burn",
  "service": "checkout",
  "severity": "SEV-2",
  "metric": "error_rate",
  "value": 0.17,
  "threshold": 0.05,
  "description": "5xx rate exceeded the alert threshold",
  "respond": true
}
```

The Pub/Sub message ID is hashed into a stable incident ID, making redelivery idempotent. Configure the push subscription to use an OIDC service account and require authenticated Cloud Run invocation; the endpoint intentionally relies on the platform to verify the push identity.

Example deployment wiring:

```bash
gcloud pubsub subscriptions create sentinelops-alerts-push \
  --topic=service-alerts \
  --push-endpoint="https://SENTINELOPS_URL/api/events/pubsub" \
  --push-auth-service-account=sentinelops-pubsub@PROJECT_ID.iam.gserviceaccount.com
```

## Firestore state

Configure:

```env
SENTINELOPS_STATE_BACKEND=firestore
SENTINELOPS_GCP_PROJECT=my-project
SENTINELOPS_FIRESTORE_DATABASE=(default)
SENTINELOPS_FIRESTORE_COLLECTION_PREFIX=sentinelops
```

Firestore stores:

- incident snapshots, including pending approvals and verification results
- transactionally appended hash-chain events with one stream head per incident
- atomic idempotency claims and completed remediation results

This allows a new Cloud Run instance to retrieve an incident awaiting approval and resume its exact pending action. The Firestore identity needs access only to collections using the configured prefix.

## Safety boundary

The provider adapters do not change the authorization model:

1. read tools collect telemetry;
2. OpenAI returns schema-constrained findings and proposals;
3. the deterministic Reviewer and PolicyEngine validate them;
4. the Remediator claims the idempotency key;
5. only then is a provider mutation called;
6. the Verifier rereads provider telemetry before declaring recovery.

Provider SDK exceptions are converted to failed tool results, causing escalation rather than optimistic success.
