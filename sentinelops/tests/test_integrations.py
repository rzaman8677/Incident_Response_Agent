import base64
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from fastapi.testclient import TestClient

import sentinelops.api as api_module
from sentinelops.core import (
    AutonomyMode,
    CloudSimulator,
    EventStore,
    IncidentStatus,
    Settings,
    ToolRegistry,
    ToolResult,
)
from sentinelops.llm import LLMConfig, OpenAIReasoner
from sentinelops.orchestrator import SentinelOrchestrator
from sentinelops.persistence import (
    FirestoreEventStore,
    FirestoreExecutionLedger,
    FirestoreIncidentStore,
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


def test_missing_cloudwatch_telemetry_fails_closed():
    class EmptyCloudWatch(FakeCloudWatch):
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": []}

    provider = AwsEcsProvider(
        region="us-east-1",
        cluster="production",
        logs_client=FakeCloudWatchLogs(),
        cloudwatch_client=EmptyCloudWatch(),
        ecs_client=FakeEcs(),
    )
    result = ToolRegistry(provider).execute(
        "get_service_health", {"service": "checkout"}
    )
    assert not result.ok
    assert "no ErrorRate datapoints" in result.message


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

    def list_namespaced_replica_set(self, namespace, label_selector):
        previous = SimpleNamespace(
            metadata=SimpleNamespace(
                annotations={"deployment.kubernetes.io/revision": "3"}
            ),
            spec=SimpleNamespace(template=SimpleNamespace(name="revision-3-template")),
        )
        return SimpleNamespace(items=[previous])


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
        api_client=SimpleNamespace(
            sanitize_for_serialization=lambda value: {"name": value.name}
        ),
    )
    metrics = provider.metrics("checkout")
    assert metrics["healthy_replicas"] == 3
    assert metrics["cpu_percent"] == 50.0
    assert provider.service_health("checkout")["healthy"]
    assert "checkout-abc" in provider.query_logs("checkout")["logs"][0]
    provider.restart_service("checkout")
    provider.scale_service("checkout", 5)
    rollback = provider.rollback_deployment("checkout")
    assert apps.patches[0][0] == "deployment"
    assert apps.patches[1][1]["spec"]["replicas"] == 5
    assert apps.patches[2][1]["spec"]["template"] == {"name": "revision-3-template"}
    assert rollback["to_revision"] == 3


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


class FakeAlreadyExists(Exception):
    pass


class FakeSnapshot:
    def __init__(self, data):
        self._data = deepcopy(data)
        self.exists = data is not None

    def to_dict(self):
        return deepcopy(self._data)


class FakeDocument:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def get(self, transaction=None):
        return FakeSnapshot(self.client.data.get(self.path))

    def set(self, data, merge=False):
        current = self.client.data.get(self.path, {}) if merge else {}
        self.client.data[self.path] = {**deepcopy(current), **deepcopy(data)}

    def create(self, data):
        if self.path in self.client.data:
            raise FakeAlreadyExists()
        self.set(data)

    def collection(self, name):
        return FakeCollection(self.client, (*self.path, name))


class FakeQuery:
    def __init__(self, collection, field, direction=None):
        self.collection = collection
        self.field = field
        self.direction = direction

    def stream(self):
        prefix = self.collection.path
        rows = [
            value
            for path, value in self.collection.client.data.items()
            if len(path) == len(prefix) + 1 and path[: len(prefix)] == prefix
        ]
        reverse = self.direction == "DESCENDING"
        rows.sort(key=lambda item: item.get(self.field, 0), reverse=reverse)
        return [FakeSnapshot(row) for row in rows]


class FakeCollection:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def document(self, document_id):
        return FakeDocument(self.client, (*self.path, document_id))

    def order_by(self, field, direction=None):
        return FakeQuery(self, field, direction)


class FakeTransaction:
    def set(self, reference, data, merge=False):
        reference.set(data, merge=merge)


class FakeFirestoreClient:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return FakeCollection(self, (name,))

    def transaction(self):
        return FakeTransaction()


def passthrough_transactional(function):
    return function


def firestore_state(client):
    return (
        FirestoreIncidentStore(client, "test_incidents"),
        FirestoreEventStore(
            client,
            "test_event_streams",
            transactional=passthrough_transactional,
        ),
        FirestoreExecutionLedger(
            client,
            "test_execution_ledger",
            already_exists_error=FakeAlreadyExists,
        ),
    )


def test_firestore_persists_approval_trace_and_idempotency_across_restart():
    client = FakeFirestoreClient()
    provider = CloudSimulator()
    incidents, events, ledger = firestore_state(client)
    first = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED),
        provider=provider,
        incident_store=incidents,
        event_store=events,
        execution_ledger=ledger,
        reasoner=deterministic_reasoner(),
    )
    provider.inject_fault("checkout", "bad_deployment")
    incident = first.create_incident(
        "checkout production SLO violation",
        "checkout",
        signals=[
            api_module.build_signal(
                "checkout",
                "error_rate",
                0.34,
                0.05,
                "regression after deployment",
            )
        ],
    )
    incident = first.respond(incident.id)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    plan_hash = incident.pending_plan_hash

    restarted_incidents, restarted_events, restarted_ledger = firestore_state(client)
    restarted = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED),
        provider=provider,
        incident_store=restarted_incidents,
        event_store=restarted_events,
        execution_ledger=restarted_ledger,
        reasoner=deterministic_reasoner(),
    )
    result = restarted.approve(
        incident.id,
        approver="on-call@example.com",
        plan_hash=plan_hash,
    )

    assert result.status is IncidentStatus.RESOLVED
    assert result.approval["approver"] == "on-call@example.com"
    assert restarted.trace(incident.id)["verified"]
    event_types = [
        event["event_type"] for event in restarted.trace(incident.id)["events"]
    ]
    assert "human.approved" in event_types
    action_key = result.executed_actions[0]["action"]["idempotency_key"]
    acquired, prior = restarted_ledger.acquire(action_key)
    assert not acquired and prior and prior.ok

    event_paths = sorted(
        path
        for path in client.data
        if path[:3] == ("test_event_streams", incident.id, "events")
    )
    client.data.pop(event_paths[-1])
    assert not restarted_events.verify(incident.id)


def test_gcp_deployment_wires_cloud_run_pubsub_firestore_and_secret_manager():
    terraform = (Path(__file__).parents[1] / "deploy" / "gcp" / "main.tf").read_text()
    assert 'resource "google_cloud_run_v2_service" "sentinelops"' in terraform
    assert 'resource "google_pubsub_subscription" "alerts_push"' in terraform
    assert 'resource "google_firestore_database" "state"' in terraform
    assert 'name  = "SENTINELOPS_STATE_BACKEND"' in terraform
    assert 'value = "firestore"' in terraform
    assert 'name  = "SENTINELOPS_INFRA_BACKEND"' in terraform
    assert 'value = "gcp_cloud_run"' in terraform
    assert 'name = "OPENAI_API_KEY"' in terraform
    assert "oidc_token {" in terraform
    assert 'role     = "roles/run.invoker"' in terraform
    assert (
        'resource "google_pubsub_topic_iam_member" "dead_letter_publisher"' in terraform
    )
