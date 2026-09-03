from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AwsEcsProvider:
    """CloudWatch telemetry plus narrowly scoped ECS remediation operations."""

    backend_name = "aws_ecs"

    def __init__(
        self,
        *,
        region: str,
        cluster: str,
        log_group_template: str = "/aws/ecs/{service}",
        metric_namespace: str = "SentinelOps/Services",
        lookback_minutes: int = 15,
        logs_client: Any | None = None,
        cloudwatch_client: Any | None = None,
        ecs_client: Any | None = None,
    ) -> None:
        if not cluster:
            raise ValueError("SENTINELOPS_AWS_ECS_CLUSTER is required")
        self.region = region
        self.cluster = cluster
        self.log_group_template = log_group_template
        self.metric_namespace = metric_namespace
        self.lookback_minutes = lookback_minutes
        if logs_client is None or cloudwatch_client is None or ecs_client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise RuntimeError(
                    "install sentinelops-agent[aws] for aws_ecs"
                ) from exc
            logs_client = logs_client or boto3.client("logs", region_name=region)
            cloudwatch_client = cloudwatch_client or boto3.client(
                "cloudwatch", region_name=region
            )
            ecs_client = ecs_client or boto3.client("ecs", region_name=region)
        self.logs_client = logs_client
        self.cloudwatch = cloudwatch_client
        self.ecs = ecs_client

    def _describe(self, service: str) -> dict[str, Any]:
        response = self.ecs.describe_services(cluster=self.cluster, services=[service])
        services = response.get("services", [])
        if not services:
            raise ValueError(f"ECS service not found: {service}")
        failures = response.get("failures", [])
        if failures:
            raise RuntimeError(
                f"ECS describe failure: {failures[0].get('reason', 'unknown')}"
            )
        return services[0]

    def _metric(self, service: str, name: str, statistic: str = "Average") -> float:
        end = _utcnow()
        dimensions = [
            {"Name": "ClusterName", "Value": self.cluster},
            {"Name": "ServiceName", "Value": service},
        ]
        response = self.cloudwatch.get_metric_statistics(
            Namespace=self.metric_namespace,
            MetricName=name,
            Dimensions=dimensions,
            StartTime=end - timedelta(minutes=self.lookback_minutes),
            EndTime=end,
            Period=max(60, self.lookback_minutes * 60),
            Statistics=[statistic],
        )
        points = response.get("Datapoints", [])
        if not points:
            raise RuntimeError(
                f"CloudWatch returned no {name} datapoints for {service}"
            )
        latest = max(
            points,
            key=lambda point: point.get("Timestamp", datetime.min.replace(tzinfo=UTC)),
        )
        return float(latest.get(statistic, 0.0))

    def metrics(self, service: str) -> dict[str, Any]:
        state = self._describe(service)
        return {
            "service": service,
            "error_rate": self._metric(service, "ErrorRate"),
            "p95_latency_ms": self._metric(service, "P95LatencyMilliseconds"),
            "cpu_percent": self._metric(service, "CPUUtilization"),
            "replicas": int(state.get("desiredCount", 0)),
            "healthy_replicas": int(state.get("runningCount", 0)),
            "deployment": state.get("taskDefinition", ""),
            "source": "cloudwatch+ecs",
        }

    def query_logs(self, service: str, contains: str = "") -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "logGroupName": self.log_group_template.format(service=service),
            "startTime": int(
                (_utcnow() - timedelta(minutes=self.lookback_minutes)).timestamp()
                * 1000
            ),
            "limit": 50,
        }
        if contains:
            kwargs["filterPattern"] = contains
        response = self.logs_client.filter_log_events(**kwargs)
        return {
            "service": service,
            "logs": [event.get("message", "") for event in response.get("events", [])],
            "source": "cloudwatch_logs",
        }

    def service_health(self, service: str) -> dict[str, Any]:
        values = self.metrics(service)
        values["healthy"] = bool(
            values["error_rate"] < 0.05
            and values["p95_latency_ms"] < 500
            and values["healthy_replicas"] == values["replicas"]
        )
        return values

    def restart_service(self, service: str) -> dict[str, Any]:
        self.ecs.update_service(
            cluster=self.cluster, service=service, forceNewDeployment=True
        )
        return {
            "service": service,
            "accepted": True,
            "operation": "ecs_force_new_deployment",
        }

    def scale_service(self, service: str, replicas: int) -> dict[str, Any]:
        if not 1 <= replicas <= 20:
            raise ValueError("replicas must be between 1 and 20")
        self.ecs.update_service(
            cluster=self.cluster, service=service, desiredCount=replicas
        )
        return {
            "service": service,
            "accepted": True,
            "operation": "ecs_scale",
            "replicas": replicas,
        }

    def rollback_deployment(self, service: str) -> dict[str, Any]:
        state = self._describe(service)
        current = state.get("taskDefinition", "")
        family = current.rsplit("/", 1)[-1].rsplit(":", 1)[0]
        response = self.ecs.list_task_definitions(
            familyPrefix=family, status="ACTIVE", sort="DESC", maxResults=10
        )
        previous = next(
            (arn for arn in response.get("taskDefinitionArns", []) if arn != current),
            None,
        )
        if not previous:
            raise RuntimeError(f"no previous active task definition found for {family}")
        self.ecs.update_service(
            cluster=self.cluster, service=service, taskDefinition=previous
        )
        return {
            "service": service,
            "accepted": True,
            "operation": "ecs_rollback",
            "from_task_definition": current,
            "to_task_definition": previous,
        }


class GcpCloudRunProvider:
    """Cloud Logging/Monitoring telemetry plus Cloud Run v2 control operations."""

    backend_name = "gcp_cloud_run"

    DEFAULT_METRICS: ClassVar[dict[str, str]] = {
        "error_rate": "custom.googleapis.com/sentinelops/error_rate",
        "p95_latency_ms": "custom.googleapis.com/sentinelops/p95_latency_ms",
        "cpu_percent": "custom.googleapis.com/sentinelops/cpu_percent",
    }

    def __init__(
        self,
        *,
        project: str,
        region: str,
        lookback_minutes: int = 15,
        logging_client: Any | None = None,
        monitoring_client: Any | None = None,
        services_client: Any | None = None,
        revisions_client: Any | None = None,
        env_var_factory: Any | None = None,
        traffic_target_factory: Any | None = None,
    ) -> None:
        if not project or not region:
            raise ValueError(
                "SENTINELOPS_GCP_PROJECT and SENTINELOPS_GCP_REGION are required"
            )
        self.project = project
        self.region = region
        self.lookback_minutes = lookback_minutes
        if any(
            client is None
            for client in (
                logging_client,
                monitoring_client,
                services_client,
                revisions_client,
            )
        ):
            try:
                from google.cloud import logging_v2, monitoring_v3, run_v2
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise RuntimeError(
                    "install sentinelops-agent[gcp] for gcp_cloud_run"
                ) from exc
            logging_client = logging_client or logging_v2.Client(project=project)
            monitoring_client = monitoring_client or monitoring_v3.MetricServiceClient()
            services_client = services_client or run_v2.ServicesClient()
            revisions_client = revisions_client or run_v2.RevisionsClient()
        self.logging = logging_client
        self.monitoring = monitoring_client
        self.services = services_client
        self.revisions = revisions_client
        self.env_var_factory = env_var_factory
        self.traffic_target_factory = traffic_target_factory

    def _service_name(self, service: str) -> str:
        return f"projects/{self.project}/locations/{self.region}/services/{service}"

    @staticmethod
    def _point_value(point: Any) -> float:
        value = point.value
        pb = getattr(value, "_pb", value)
        field = pb.WhichOneof("value") if hasattr(pb, "WhichOneof") else None
        if field:
            return float(getattr(value, field))
        for candidate in ("double_value", "int64_value"):
            if hasattr(value, candidate):
                return float(getattr(value, candidate))
        return 0.0

    def _metric(self, service: str, metric_type: str) -> float:
        end = _utcnow()
        request = {
            "name": f"projects/{self.project}",
            "filter": (
                f'metric.type = "{metric_type}" '
                f'AND resource.labels.service_name = "{service}"'
            ),
            "interval": {
                "start_time": end - timedelta(minutes=self.lookback_minutes),
                "end_time": end,
            },
            "view": "FULL",
            "page_size": 1,
        }
        for series in self.monitoring.list_time_series(request=request):
            if series.points:
                return self._point_value(series.points[0])
        raise RuntimeError(
            f"Cloud Monitoring returned no datapoints for {metric_type} on {service}"
        )

    def _service(self, service: str) -> Any:
        return self.services.get_service(request={"name": self._service_name(service)})

    def metrics(self, service: str) -> dict[str, Any]:
        state = self._service(service)
        scaling = state.template.scaling
        configured = {
            key: os.getenv(f"SENTINELOPS_GCP_METRIC_{key.upper()}", value)
            for key, value in self.DEFAULT_METRICS.items()
        }
        desired = int(scaling.min_instance_count or 0)
        return {
            "service": service,
            "error_rate": self._metric(service, configured["error_rate"]),
            "p95_latency_ms": self._metric(service, configured["p95_latency_ms"]),
            "cpu_percent": self._metric(service, configured["cpu_percent"]),
            "replicas": desired,
            "healthy_replicas": desired if state.latest_ready_revision else 0,
            "deployment": state.latest_ready_revision,
            "source": "cloud_monitoring+cloud_run",
        }

    def query_logs(self, service: str, contains: str = "") -> dict[str, Any]:
        since = (_utcnow() - timedelta(minutes=self.lookback_minutes)).isoformat()
        filters = [
            'resource.type="cloud_run_revision"',
            f'resource.labels.service_name="{service}"',
            f'timestamp>="{since}"',
        ]
        if contains:
            escaped = contains.replace('"', '\\"')
            filters.append(f'textPayload:"{escaped}"')
        entries = self.logging.list_entries(
            filter_=" AND ".join(filters), order_by="timestamp desc", page_size=50
        )
        logs = []
        for entry in entries:
            payload = getattr(entry, "payload", "")
            logs.append(payload if isinstance(payload, str) else str(payload))
            if len(logs) >= 50:
                break
        return {"service": service, "logs": logs, "source": "cloud_logging"}

    def service_health(self, service: str) -> dict[str, Any]:
        values = self.metrics(service)
        values["healthy"] = bool(
            values["deployment"]
            and values["error_rate"] < 0.05
            and values["p95_latency_ms"] < 500
            and values["healthy_replicas"] == values["replicas"]
        )
        return values

    def _update(self, service_obj: Any, paths: list[str]) -> None:
        operation = self.services.update_service(
            request={"service": service_obj, "update_mask": {"paths": paths}}
        )
        operation.result(timeout=120)

    @staticmethod
    def _revision_timestamp(revision: Any) -> float:
        value = getattr(revision, "create_time", None)
        return (
            float(value.timestamp())
            if hasattr(value, "timestamp")
            else float(value or 0)
        )

    def restart_service(self, service: str) -> dict[str, Any]:
        factory = self.env_var_factory
        if factory is None:
            try:
                from google.cloud.run_v2.types import EnvVar
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "install sentinelops-agent[gcp] for Cloud Run mutation tools"
                ) from exc
            factory = EnvVar
        state = self._service(service)
        stamp = _utcnow().isoformat()
        for container in state.template.containers:
            existing = next(
                (
                    item
                    for item in container.env
                    if item.name == "SENTINELOPS_RESTARTED_AT"
                ),
                None,
            )
            if existing:
                existing.value = stamp
            else:
                container.env.append(
                    factory(name="SENTINELOPS_RESTARTED_AT", value=stamp)
                )
        self._update(state, ["template.containers"])
        return {
            "service": service,
            "accepted": True,
            "operation": "cloud_run_new_revision",
        }

    def scale_service(self, service: str, replicas: int) -> dict[str, Any]:
        if not 1 <= replicas <= 20:
            raise ValueError("replicas must be between 1 and 20")
        state = self._service(service)
        state.template.scaling.min_instance_count = replicas
        if (
            state.template.scaling.max_instance_count
            and state.template.scaling.max_instance_count < replicas
        ):
            state.template.scaling.max_instance_count = replicas
        self._update(state, ["template.scaling"])
        return {
            "service": service,
            "accepted": True,
            "operation": "cloud_run_scale_floor",
            "replicas": replicas,
        }

    def rollback_deployment(self, service: str) -> dict[str, Any]:
        factory = self.traffic_target_factory
        allocation_type: Any = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
        if factory is None:
            try:
                from google.cloud.run_v2.types import (
                    TrafficTarget,
                    TrafficTargetAllocationType,
                )
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "install sentinelops-agent[gcp] for Cloud Run mutation tools"
                ) from exc
            factory = TrafficTarget
            allocation_type = (
                TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
            )
        state = self._service(service)
        revisions = sorted(
            self.revisions.list_revisions(
                request={"parent": self._service_name(service), "page_size": 10}
            ),
            key=self._revision_timestamp,
            reverse=True,
        )
        previous = next(
            (
                revision
                for revision in revisions
                if revision.name != state.latest_ready_revision
            ),
            None,
        )
        if previous is None:
            raise RuntimeError(f"no previous Cloud Run revision found for {service}")
        state.traffic = [
            factory(
                type_=allocation_type,
                revision=previous.name.rsplit("/", 1)[-1],
                percent=100,
            )
        ]
        self._update(state, ["traffic"])
        return {
            "service": service,
            "accepted": True,
            "operation": "cloud_run_rollback",
            "to_revision": previous.name,
        }


class KubernetesProvider:
    """Kubernetes deployment, pod-log, metrics-server, and rollout adapter."""

    backend_name = "kubernetes"

    def __init__(
        self,
        *,
        namespace: str = "default",
        label_key: str = "app.kubernetes.io/name",
        apps_api: Any | None = None,
        core_api: Any | None = None,
        custom_api: Any | None = None,
        api_client: Any | None = None,
    ) -> None:
        self.namespace = namespace
        self.label_key = label_key
        if any(
            client is None for client in (apps_api, core_api, custom_api, api_client)
        ):
            try:
                from kubernetes import client, config
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise RuntimeError(
                    "install sentinelops-agent[kubernetes] for kubernetes"
                ) from exc
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(
                    context=os.getenv("SENTINELOPS_K8S_CONTEXT") or None
                )
            api_client = api_client or client.ApiClient()
            apps_api = apps_api or client.AppsV1Api(api_client)
            core_api = core_api or client.CoreV1Api(api_client)
            custom_api = custom_api or client.CustomObjectsApi(api_client)
        self.apps = apps_api
        self.core = core_api
        self.custom = custom_api
        self.api_client = api_client

    def _deployment(self, service: str) -> Any:
        return self.apps.read_namespaced_deployment(service, self.namespace)

    @staticmethod
    def _cpu_millicores(value: str) -> float:
        if value.endswith("n"):
            return float(value[:-1]) / 1_000_000
        if value.endswith("u"):
            return float(value[:-1]) / 1_000
        if value.endswith("m"):
            return float(value[:-1])
        return float(value) * 1000

    def _cpu_percent(self, service: str) -> float:
        try:
            response = self.custom.list_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=self.namespace,
                plural="pods",
                label_selector=f"{self.label_key}={service}",
            )
        except Exception:  # noqa: BLE001 - metrics-server is optional
            return 0.0
        total = 0.0
        containers = 0
        for pod in response.get("items", []):
            for container in pod.get("containers", []):
                total += self._cpu_millicores(
                    container.get("usage", {}).get("cpu", "0")
                )
                containers += 1
        return total / max(containers, 1) / 10

    def metrics(self, service: str) -> dict[str, Any]:
        deployment = self._deployment(service)
        annotations = deployment.metadata.annotations or {}
        required = ("sentinelops.ai/error-rate", "sentinelops.ai/p95-latency-ms")
        missing = [key for key in required if key not in annotations]
        if missing:
            raise RuntimeError(
                f"deployment {service} is missing required SLO annotations: {', '.join(missing)}"
            )
        replicas = int(deployment.spec.replicas or 0)
        return {
            "service": service,
            "error_rate": float(annotations.get("sentinelops.ai/error-rate", 0.0)),
            "p95_latency_ms": float(
                annotations.get("sentinelops.ai/p95-latency-ms", 0.0)
            ),
            "cpu_percent": self._cpu_percent(service),
            "replicas": replicas,
            "healthy_replicas": int(deployment.status.available_replicas or 0),
            "deployment": annotations.get("deployment.kubernetes.io/revision", ""),
            "source": "kubernetes_api",
        }

    def query_logs(self, service: str, contains: str = "") -> dict[str, Any]:
        pods = self.core.list_namespaced_pod(
            self.namespace,
            label_selector=f"{self.label_key}={service}",
            limit=10,
        )
        lines: list[str] = []
        for pod in pods.items:
            try:
                text = self.core.read_namespaced_pod_log(
                    pod.metadata.name,
                    self.namespace,
                    tail_lines=50,
                    timestamps=True,
                )
            except Exception as exc:  # noqa: BLE001 - one failed pod must not hide other logs
                lines.append(f"LOG_READ_ERROR pod={pod.metadata.name} error={exc}")
                continue
            for line in text.splitlines():
                if not contains or contains.lower() in line.lower():
                    lines.append(f"pod={pod.metadata.name} {line}")
        return {
            "service": service,
            "logs": lines[-50:],
            "source": "kubernetes_pod_logs",
        }

    def service_health(self, service: str) -> dict[str, Any]:
        values = self.metrics(service)
        values["healthy"] = bool(
            values["error_rate"] < 0.05
            and values["p95_latency_ms"] < 500
            and values["healthy_replicas"] == values["replicas"]
        )
        return values

    def restart_service(self, service: str) -> dict[str, Any]:
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "sentinelops.ai/restarted-at": _utcnow().isoformat()
                        }
                    }
                }
            }
        }
        self.apps.patch_namespaced_deployment(service, self.namespace, body)
        return {
            "service": service,
            "accepted": True,
            "operation": "kubernetes_rollout_restart",
        }

    def scale_service(self, service: str, replicas: int) -> dict[str, Any]:
        if not 1 <= replicas <= 20:
            raise ValueError("replicas must be between 1 and 20")
        self.apps.patch_namespaced_deployment_scale(
            service,
            self.namespace,
            {"spec": {"replicas": replicas}},
        )
        return {
            "service": service,
            "accepted": True,
            "operation": "kubernetes_scale",
            "replicas": replicas,
        }

    def rollback_deployment(self, service: str) -> dict[str, Any]:
        deployment = self._deployment(service)
        selector = ",".join(
            f"{key}={value}"
            for key, value in deployment.spec.selector.match_labels.items()
        )
        sets = self.apps.list_namespaced_replica_set(
            self.namespace, label_selector=selector
        ).items
        current_revision = int(
            (deployment.metadata.annotations or {}).get(
                "deployment.kubernetes.io/revision", "0"
            )
        )
        candidates = []
        for replica_set in sets:
            revision = int(
                (replica_set.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision", "0"
                )
            )
            if revision and revision < current_revision:
                candidates.append((revision, replica_set))
        if not candidates:
            raise RuntimeError(f"no previous ReplicaSet revision found for {service}")
        revision, previous = max(candidates, key=lambda item: item[0])
        template = self.api_client.sanitize_for_serialization(previous.spec.template)
        self.apps.patch_namespaced_deployment(
            service, self.namespace, {"spec": {"template": template}}
        )
        return {
            "service": service,
            "accepted": True,
            "operation": "kubernetes_rollback",
            "to_revision": revision,
        }


def provider_from_env() -> Any:
    """Create the selected infrastructure adapter without importing unused SDKs."""

    backend = os.getenv("SENTINELOPS_INFRA_BACKEND", "simulator").strip().lower()
    lookback = int(os.getenv("SENTINELOPS_TELEMETRY_LOOKBACK_MINUTES", "15"))
    if backend == "simulator":
        from .core import CloudSimulator

        return CloudSimulator()
    if backend == "aws_ecs":
        return AwsEcsProvider(
            region=os.getenv("SENTINELOPS_AWS_REGION", "us-east-1"),
            cluster=os.getenv("SENTINELOPS_AWS_ECS_CLUSTER", ""),
            log_group_template=os.getenv(
                "SENTINELOPS_AWS_LOG_GROUP_TEMPLATE", "/aws/ecs/{service}"
            ),
            metric_namespace=os.getenv(
                "SENTINELOPS_AWS_METRIC_NAMESPACE", "SentinelOps/Services"
            ),
            lookback_minutes=lookback,
        )
    if backend == "gcp_cloud_run":
        return GcpCloudRunProvider(
            project=os.getenv(
                "SENTINELOPS_GCP_PROJECT", os.getenv("GOOGLE_CLOUD_PROJECT", "")
            ),
            region=os.getenv("SENTINELOPS_GCP_REGION", ""),
            lookback_minutes=lookback,
        )
    if backend in {"kubernetes", "eks", "gke"}:
        return KubernetesProvider(
            namespace=os.getenv("SENTINELOPS_K8S_NAMESPACE", "default"),
            label_key=os.getenv("SENTINELOPS_K8S_LABEL_KEY", "app.kubernetes.io/name"),
        )
    raise ValueError(f"unsupported SENTINELOPS_INFRA_BACKEND: {backend}")
