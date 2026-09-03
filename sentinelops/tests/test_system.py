import json

import pytest
from fastapi.testclient import TestClient

import sentinelops.api as api_module
from sentinelops.config import settings_from_env
from sentinelops.core import (
    ActionProposal,
    AutonomyMode,
    CloudSimulator,
    EventStore,
    IncidentStatus,
    PolicyEngine,
    RiskLevel,
    RunbookRetriever,
    Settings,
)
from sentinelops.diagnostics import run_diagnostics
from sentinelops.evals import run_evals
from sentinelops.llm import LLMConfig, LLMError, LLMResult, OpenAIReasoner
from sentinelops.orchestrator import SentinelOrchestrator, create_demo_incident

client = TestClient(api_module.app)


def deterministic_reasoner():
    return OpenAIReasoner(LLMConfig(backend="deterministic"))


def proposal(**overrides):
    values = {
        "tool": "restart_service",
        "args": {"service": "checkout"},
        "rationale": "recover",
        "expected_effect": "healthy",
        "risk": RiskLevel.MEDIUM,
        "blast_radius": 1,
        "confidence": 0.95,
    }
    values.update(overrides)
    return ActionProposal(**values)


def test_policy_modes_and_hard_high_risk_gate():
    assisted = PolicyEngine(Settings(AutonomyMode.ASSISTED)).evaluate(proposal())
    assert assisted.requires_approval and not assisted.allowed
    autonomous = PolicyEngine(Settings(AutonomyMode.AUTONOMOUS)).evaluate(proposal())
    assert autonomous.allowed and not autonomous.requires_approval
    high = PolicyEngine(Settings(AutonomyMode.AUTONOMOUS)).evaluate(
        proposal(risk=RiskLevel.HIGH)
    )
    assert high.requires_approval and not high.allowed


def test_runtime_policy_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("SENTINELOPS_AUTONOMY", "autonomous")
    monkeypatch.setenv("SENTINELOPS_CONFIDENCE_THRESHOLD", "0.91")
    monkeypatch.setenv("SENTINELOPS_MAX_BLAST_RADIUS", "3")
    monkeypatch.setenv("SENTINELOPS_ACTION_BUDGET", "2")
    monkeypatch.setenv("SENTINELOPS_VERIFICATION_ATTEMPTS", "9")
    monkeypatch.setenv("SENTINELOPS_VERIFICATION_INTERVAL_SECONDS", "2.5")
    settings = settings_from_env()
    assert settings.autonomy_mode is AutonomyMode.AUTONOMOUS
    assert settings.autonomous_confidence_threshold == 0.91
    assert settings.max_blast_radius == 3
    assert settings.action_budget == 2
    assert settings.verification_attempts == 9
    assert settings.verification_interval_seconds == 2.5


def test_forced_openai_mode_requires_local_configuration(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    reasoner = OpenAIReasoner(LLMConfig(backend="openai"))
    assert not reasoner.enabled
    with pytest.raises(LLMError):
        SentinelOrchestrator(reasoner=reasoner)


def test_assisted_flow_pauses_then_resolves():
    app = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED), reasoner=deterministic_reasoner()
    )
    incident = create_demo_incident(app, "bad_deployment", "checkout")
    app.respond(incident.id)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.pending_actions[0]["tool"] == "rollback_deployment"
    assert incident.pending_plan_hash
    assert incident.executed_actions == []
    app.approve(
        incident.id,
        approver="on-call@example.com",
        plan_hash=incident.pending_plan_hash,
    )
    assert incident.status is IncidentStatus.RESOLVED
    assert incident.approval["approver"] == "on-call@example.com"
    assert incident.verification["slo_checks"][0]["passed"]
    assert app.events.verify(incident.id)


def test_tampered_pending_plan_cannot_be_approved():
    app = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED), reasoner=deterministic_reasoner()
    )
    incident = create_demo_incident(app, "bad_deployment", "checkout")
    app.respond(incident.id)
    original_hash = incident.pending_plan_hash
    incident.pending_actions[0]["args"]["service"] = "payments"

    with pytest.raises(ValueError, match="integrity"):
        app.approve(
            incident.id,
            approver="on-call@example.com",
            plan_hash=original_hash,
        )
    assert incident.executed_actions == []


def test_autonomous_capacity_recovery():
    app = SentinelOrchestrator(
        Settings(AutonomyMode.AUTONOMOUS, 0.8), reasoner=deterministic_reasoner()
    )
    incident = create_demo_incident(app, "capacity", "payments")
    app.respond(incident.id)
    assert incident.status is IncidentStatus.RESOLVED
    assert incident.executed_actions[0]["action"]["tool"] == "scale_service"


def test_hash_chain_and_runbook_retrieval():
    store = EventStore()
    first = store.append("i-1", "created", {"x": 1})
    second = store.append("i-1", "investigated", {"x": 2})
    assert second.previous_hash == first.hash and store.verify("i-1")
    store._events["i-1"][0].payload["x"] = 999
    assert not store.verify("i-1")
    clean = EventStore()
    clean.append("i-2", "created", {"x": 1})
    clean.append("i-2", "investigated", {"x": 2})
    clean._events["i-2"].pop()
    assert not clean.verify("i-2")
    assert (
        RunbookRetriever().search("errors after deployment rollback", "checkout")[0].id
        == "RB-DEPLOY-001"
    )


def test_diagnostics_are_ready(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SENTINELOPS_AGENT_BACKEND", "auto")
    report = run_diagnostics()
    assert report["ok"]
    assert all(check["ok"] for check in report["checks"].values())
    assert "rollback_deployment" in report["checks"]["tool_registry"]["detail"]
    assert report["checks"]["llm"]["detail"]["active_backend"] == "deterministic"


def test_api_assisted_incident_flow(monkeypatch):
    control_plane = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED), reasoner=deterministic_reasoner()
    )
    monkeypatch.setattr(api_module, "control_plane", control_plane)

    assert client.get("/health").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["ok"]

    fault = client.post(
        "/api/simulator/faults", json={"service": "checkout", "fault": "bad_deployment"}
    )
    assert fault.status_code == 200

    created = client.post(
        "/api/incidents",
        json={
            "title": "checkout production SLO violation",
            "service": "checkout",
            "metric": "error_rate",
            "value": 0.34,
            "threshold": 0.05,
            "description": "error spike started immediately after the latest deployment",
        },
    )
    assert created.status_code == 200
    incident_id = created.json()["id"]

    response = client.post(f"/api/incidents/{incident_id}/respond")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_approval"
    assert response.json()["pending_actions"][0]["tool"] == "rollback_deployment"
    assert response.json()["executed_actions"] == []
    plan_hash = response.json()["pending_plan_hash"]

    rejected = client.post(
        f"/api/incidents/{incident_id}/approve",
        json={"approver": "on-call@example.com", "plan_hash": "0" * 64},
    )
    assert rejected.status_code == 409
    assert control_plane.get(incident_id).executed_actions == []

    approved = client.post(
        f"/api/incidents/{incident_id}/approve",
        json={"approver": "on-call@example.com", "plan_hash": plan_hash},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "resolved"
    assert approved.json()["approval"]["plan_hash"] == plan_hash

    trace = client.get(f"/api/incidents/{incident_id}/trace")
    assert trace.status_code == 200 and trace.json()["verified"]

    metrics = client.get("/api/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["incidents_total"] == 1
    assert metrics.json()["trace_integrity_rate"] == 1.0


def test_openai_responses_adapter_uses_strict_schema():
    class FakeResponse:
        id = "resp_test"
        output_text = json.dumps({"ok": True, "message": "connected"})

    class FakeResponses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

    fake_client = FakeClient()
    reasoner = OpenAIReasoner(
        LLMConfig(backend="openai", model="gpt-test"),
        client=fake_client,
        api_key="test-key",
    )
    result = reasoner.generate_json(
        schema_name="adapter_test",
        instructions="Return JSON.",
        payload={"hello": "world"},
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "message": {"type": "string"}},
            "required": ["ok", "message"],
            "additionalProperties": False,
        },
    )
    call = fake_client.responses.calls[0]
    assert call["model"] == "gpt-test"
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    assert result.data["ok"] is True


def test_llm_investigator_and_planner_path_resolves_incident():
    class ScriptedReasoner:
        enabled = True
        config = LLMConfig(
            backend="openai", model="gpt-test", fallback_to_deterministic=True
        )

        def __init__(self):
            self.calls = []
            self.payloads = []

        def status(self):
            return {
                "requested_backend": "openai",
                "active_backend": "openai",
                "model": "gpt-test",
                "configured": True,
            }

        def generate_json(self, *, schema_name, instructions, payload, schema):
            self.calls.append(schema_name)
            self.payloads.append(payload)
            if schema_name == "sentinelops_incident_findings":
                data = {
                    "findings": [
                        {
                            "kind": "capacity_pressure",
                            "detail": "telemetry and queue logs indicate saturation",
                            "confidence": 0.94,
                        },
                        {
                            "kind": "latency_spike",
                            "detail": "p95 latency is well above the SLO",
                            "confidence": 0.90,
                        },
                    ]
                }
            else:
                data = {
                    "actions": [
                        {
                            "tool": "scale_service",
                            "service": "payments",
                            "replicas": 6,
                            "rationale": "increase capacity to reduce queueing pressure",
                            "expected_effect": "restore latency and CPU headroom",
                            "risk": "medium",
                            "blast_radius": 1,
                            "confidence": 0.92,
                        }
                    ]
                }
            return LLMResult(
                data=data,
                model="gpt-test",
                response_id=f"resp_{len(self.calls)}",
                latency_ms=12.0,
            )

    reasoner = ScriptedReasoner()
    app = SentinelOrchestrator(
        Settings(AutonomyMode.AUTONOMOUS, 0.8), reasoner=reasoner
    )
    incident = create_demo_incident(app, "capacity", "payments")
    app.respond(incident.id)

    assert reasoner.calls == [
        "sentinelops_incident_findings",
        "sentinelops_remediation_plan",
    ]
    assert reasoner.payloads[0]["metrics"]["cpu_percent"] == 97
    assert reasoner.payloads[0]["logs"]["logs"]
    assert reasoner.payloads[0]["runbooks"]
    assert reasoner.payloads[1]["available_write_tools"] == [
        "rollback_deployment",
        "scale_service",
        "restart_service",
    ]
    assert incident.status is IncidentStatus.RESOLVED
    assert incident.executed_actions[0]["action"]["tool"] == "scale_service"
    events = app.trace(incident.id)["events"]
    assert any(event["event_type"] == "planner.llm_completed" for event in events)
    investigator = next(
        event for event in events if event["event_type"] == "investigator.completed"
    )
    assert investigator["payload"]["reasoning"]["backend"] == "openai"
    assert {
        "investigator.completed",
        "planner.created",
        "reviewer.completed",
        "remediator.executed",
        "verifier.completed",
    }.issubset({event["event_type"] for event in events})


def test_eval_gate():
    report = run_evals()
    assert report["backend"] == "deterministic"
    assert report["case_count"] == 100
    assert report["recovery_case_count"] == 75
    assert report["safety_case_count"] == 25
    assert report["case_pass_rate"] == 1.0
    assert report["resolution_rate"] == 1.0
    assert report["tool_selection_accuracy"] == 1.0
    assert report["unsafe_action_count"] == 0
    assert report["unsafe_action_rate"] == 0.0
    assert report["trace_integrity_rate"] == 1.0


def test_remediation_is_idempotent_across_duplicate_execution():
    class CountingSimulator(CloudSimulator):
        def __init__(self):
            super().__init__()
            self.restart_calls = 0

        def restart_service(self, service):
            self.restart_calls += 1
            return super().restart_service(service)

    provider = CountingSimulator()
    app = SentinelOrchestrator(
        Settings(AutonomyMode.ASSISTED),
        provider=provider,
        reasoner=deterministic_reasoner(),
    )
    incident = create_demo_incident(app, "crashloop", "catalog")
    app.respond(incident.id)
    action = app._proposal_from_dict(incident.pending_actions[0])
    app.approve(
        incident.id,
        approver="on-call@example.com",
        plan_hash=incident.pending_plan_hash,
    )
    duplicate = app.remediator.execute(incident.id, action)

    assert provider.restart_calls == 1
    assert duplicate.deduplicated
    assert duplicate.result.ok

    resolved = app.respond(incident.id)
    assert resolved.status is IncidentStatus.RESOLVED
    assert provider.restart_calls == 1


def test_verifier_polls_until_original_slo_recovers():
    class DelayedRecoveryProvider(CloudSimulator):
        def __init__(self):
            super().__init__()
            self.after_restart = False
            self.post_restart_metric_reads = 0

        def restart_service(self, service):
            self.after_restart = True
            self.post_restart_metric_reads = 0
            return {"service": service, "accepted": True}

        def service_health(self, service):
            if self.after_restart:
                return {"service": service, "healthy": True}
            return super().service_health(service)

        def metrics(self, service):
            if not self.after_restart:
                return super().metrics(service)
            self.post_restart_metric_reads += 1
            error_rate = 0.20 if self.post_restart_metric_reads == 1 else 0.01
            return {
                "service": service,
                "error_rate": error_rate,
                "p95_latency_ms": 130,
                "cpu_percent": 35,
                "replicas": 3,
                "healthy_replicas": 3,
            }

    app = SentinelOrchestrator(
        Settings(
            AutonomyMode.AUTONOMOUS,
            0.8,
            verification_attempts=3,
            verification_interval_seconds=0,
        ),
        provider=DelayedRecoveryProvider(),
        reasoner=deterministic_reasoner(),
    )
    incident = create_demo_incident(app, "crashloop", "catalog")
    app.respond(incident.id)

    assert incident.status is IncidentStatus.RESOLVED
    assert incident.verification["attempt_count"] == 2
    assert not incident.verification["observations"][0]["slo_checks"][0]["passed"]
    assert incident.verification["observations"][1]["slo_checks"][0]["passed"]
