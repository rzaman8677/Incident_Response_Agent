import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar

from fastapi.testclient import TestClient

import sentinelops.api as api_module
from sentinelops.core import (
    AutonomyMode,
    CloudSimulator,
    EventStore,
    Settings,
    ToolResult,
)
from sentinelops.llm import LLMConfig, OpenAIReasoner
from sentinelops.orchestrator import SentinelOrchestrator
from sentinelops.persistence import (
    MemoryExecutionLedger,
    MemoryIncidentStore,
    incident_from_dict,
)
from sentinelops.providers import (
    AwsEcsProvider,
    GcpCloudRunProvider,
    KubernetesProvider,
)


def deterministic_reasoner():
    return OpenAIReasoner(LLMConfig(backend="deterministic"))


class FakeEcs:
    def __init__(self):
        self.updates = []

    def describe_services(self, **kwargs):
        return {
            "services": [
                {
                    "desiredCount": 3,
                    "runningCount": 3,
                    "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/checkout:7",
                }
            ]
        }

    def list_task_definitions(self, **kwargs):
        return {
            "taskDefinitionArns": [
                "arn:aws:ecs:us-east-1:1:task-definition/checkout:7",
                "arn:aws:ecs:us-east-1:1:task-definition/checkout:6",
            ]
        }

    def update_service(self, **kwargs):
        self.updates.append(kwargs)
        return {"service": kwargs}


class FakeCloudWatch:
    VALUES: ClassVar[dict[str, float]] = {
        "ErrorRate": 0.12,
        "P95LatencyMilliseconds": 850.0,
        "CPUUtilization": 91.0,
    }

    def get_metric_statistics(self, **kwargs):
        return {
            "Datapoints": [
                {
                    "Timestamp": datetime.now(UTC),
                    "Average": self.VALUES[kwargs["MetricName"]],
                }
            ]
        }


class FakeCloudWatchLogs:
    def filter_log_events(self, **kwargs):
        return {"events": [{"message": "ERROR checkout timeout"}]}


def test_aws_cloudwatch_and_ecs_provider():
    ecs = FakeEcs()
    provider = AwsEcsProvider(
        region="us-east-1",
        cluster="production",
        logs_client=FakeCloudWatchLogs(),
        cloudwatch_client=FakeCloudWatch(),
        ecs_client=ecs,
    )

    metrics = provider.metrics("checkout")
    assert metrics["error_rate"] == 0.12
    assert metrics["healthy_replicas"] == 3
    assert provider.query_logs("checkout")["logs"] == ["ERROR checkout timeout"]
    assert not provider.service_health("checkout")["healthy"]

    provider.restart_service("checkout")
    provider.scale_service("checkout", 6)
    rollback = provider.rollback_deployment("checkout")
    assert ecs.updates[0]["forceNewDeployment"] is True
    assert ecs.updates[1]["desiredCount"] == 6
    assert rollback["to_task_definition"].endswith(":6")


class FakeMetricValue:
    def __init__(self, value):
        self.double_value = value
        self._pb = self

    def WhichOneof(self, _):
        return "double_value"


class FakeMonitoring:
    def list_time_series(self, *, request):
        metric_type = request["filter"].split('"')[1]
        values = {
            "custom.googleapis.com/sentinelops/error_rate": 0.08,
            "custom.googleapis.com/sentinelops/p95_latency_ms": 720.0,
            "custom.googleapis.com/sentinelops/cpu_percent": 88.0,
        }
        point = SimpleNamespace(value=FakeMetricValue(values[metric_type]))
        return [SimpleNamespace(points=[point])]


class FakeLogging:
    def list_entries(self, **kwargs):
        return [SimpleNamespace(payload="ERROR failed request")]


class FakeOperation:
    def result(self, timeout):
        return {"done": True, "timeout": timeout}


class FakeRunServices:
    def __init__(self):
        scaling = SimpleNamespace(min_instance_count=2, max_instance_count=10)
        container = SimpleNamespace(env=[])
        template = SimpleNamespace(scaling=scaling, containers=[container])
        self.state = SimpleNamespace(
            template=template,
            latest_ready_revision="projects/p/locations/us/services/checkout/revisions/r2",
            traffic=[],
        )
        self.updates = []

    def get_service(self, *, request):
        return self.state

    def update_service(self, *, request):
        self.updates.append(request)
        return FakeOperation()


class FakeRevisions:
    def list_revisions(self, *, request):
        newer = SimpleNamespace(
            name="projects/p/locations/us/services/checkout/revisions/r2", create_time=2
        )
        older = SimpleNamespace(
            name="projects/p/locations/us/services/checkout/revisions/r1", create_time=1
        )
        return [newer, older]


def test_gcp_logging_monitoring_and_cloud_run_provider():
    services = FakeRunServices()
    provider = GcpCloudRunProvider(
        project="p",
        region="us",
        logging_client=FakeLogging(),
        monitoring_client=FakeMonitoring(),
        services_client=services,
        revisions_client=FakeRevisions(),
        env_var_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        traffic_target_factory=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    assert provider.metrics("checkout")["cpu_percent"] == 88.0
    assert provider.query_logs("checkout")["logs"] == ["ERROR failed request"]
    provider.restart_service("checkout")
    provider.scale_service("checkout", 4)
    rollback = provider.rollback_deployment("checkout")
    assert (
        services.state.template.containers[0].env[0].name == "SENTINELOPS_RESTARTED_AT"
    )
    assert services.state.template.scaling.min_instance_count == 4
    assert rollback["to_revision"].endswith("/r1")


class FakeAppsApi:
    def __init__(self):
        metadata = SimpleNamespace(
            annotations={
                "deployment.kubernetes.io/revision": "4",
                "sentinelops.ai/error-rate": "0.03",
                "sentinelops.ai/p95-latency-ms": "240",
            }
        )
        selector = SimpleNamespace(match_labels={"app.kubernetes.io/name": "checkout"})
        spec = SimpleNamespace(replicas=3, selector=selector)
        status = SimpleNamespace(available_replicas=3)
        self.deployment = SimpleNamespace(metadata=metadata, spec=spec, status=status)
        self.patches = []

    def read_namespaced_deployment(self, name, namespace):
        return self.deployment

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append(("deployment", body))

    def patch_namespaced_deployment_scale(self, name, namespace, body):
        self.patches.append(("scale", body))


class FakeCoreApi:
    def list_namespaced_pod(self, *args, **kwargs):
        return SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name="checkout-abc"))]
        )

    def read_namespaced_pod_log(self, *args, **kwargs):
        return "2026-09-03T00:00:00Z ERROR example"


class FakeCustomApi:
    def list_namespaced_custom_object(self, **kwargs):
        return {"items": [{"containers": [{"usage": {"cpu": "500m"}}]}]}


def test_kubernetes_health_logs_restart_and_scale():
    apps = FakeAppsApi()
    provider = KubernetesProvider(
        namespace="production",
        apps_api=apps,
        core_api=FakeCoreApi(),
        custom_api=FakeCustomApi(),
        api_client=SimpleNamespace(),
    )
    metrics = provider.metrics("checkout")
    assert metrics["healthy_replicas"] == 3
    assert metrics["cpu_percent"] == 50.0
    assert provider.service_health("checkout")["healthy"]
    assert "checkout-abc" in provider.query_logs("checkout")["logs"][0]
    provider.restart_service("checkout")
    provider.scale_service("checkout", 5)
    assert apps.patches[0][0] == "deployment"
    assert apps.patches[1][1]["spec"]["replicas"] == 5


def test_pubsub_push_ingestion_is_idempotent(monkeypatch):
    control_plane = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED), reasoner=deterministic_reasoner()
    )
    monkeypatch.setattr(api_module, "control_plane", control_plane)
    client = TestClient(api_module.app)
    payload = {
        "title": "checkout high error rate",
        "service": "checkout",
        "metric": "error_rate",
        "value": 0.2,
        "threshold": 0.05,
        "description": "monitor alert",
        "respond": False,
    }
    envelope = {
        "message": {
            "messageId": "message-123",
            "data": base64.b64encode(json.dumps(payload).encode()).decode(),
        }
    }

    first = client.post("/api/events/pubsub", json=envelope)
    second = client.post("/api/events/pubsub", json=envelope)
    assert first.status_code == 200 and not first.json()["duplicate"]
    assert second.status_code == 200 and second.json()["duplicate"]
    assert first.json()["incident"]["id"] == second.json()["incident"]["id"]
    assert len(control_plane.list()) == 1


def test_memory_execution_ledger_and_incident_roundtrip():
    ledger = MemoryExecutionLedger()
    acquired, previous = ledger.acquire("action-1")
    assert acquired and previous is None
    result = ToolResult(True, {"accepted": True})
    ledger.complete("action-1", result)
    acquired, previous = ledger.acquire("action-1")
    assert not acquired and previous == result

    app = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED),
        provider=CloudSimulator(),
        incident_store=MemoryIncidentStore(),
        event_store=EventStore(),
        execution_ledger=MemoryExecutionLedger(),
        reasoner=deterministic_reasoner(),
    )
    incident = app.create_incident("test", "checkout")
    assert incident_from_dict(incident.to_dict()).to_dict() == incident.to_dict()
