import json

from fastapi.testclient import TestClient

import sentinelops.api as api_module
from sentinelops.core import ActionProposal, AutonomyMode, EventStore, IncidentStatus, PolicyEngine, RiskLevel, RunbookRetriever, Settings
from sentinelops.diagnostics import run_diagnostics
from sentinelops.evals import run_evals
from sentinelops.llm import LLMConfig, LLMResult, OpenAIReasoner
from sentinelops.orchestrator import SentinelOrchestrator, create_demo_incident


client = TestClient(api_module.app)


def deterministic_reasoner():
    return OpenAIReasoner(LLMConfig(backend="deterministic"))


def proposal(**overrides):
    values = dict(tool="restart_service", args={"service": "checkout"}, rationale="recover", expected_effect="healthy", risk=RiskLevel.MEDIUM, blast_radius=1, confidence=0.95)
    values.update(overrides)
    return ActionProposal(**values)


def test_policy_modes_and_hard_high_risk_gate():
    assisted = PolicyEngine(Settings(AutonomyMode.ASSISTED)).evaluate(proposal())
    assert assisted.requires_approval and not assisted.allowed
    autonomous = PolicyEngine(Settings(AutonomyMode.AUTONOMOUS)).evaluate(proposal())
    assert autonomous.allowed and not autonomous.requires_approval
    high = PolicyEngine(Settings(AutonomyMode.AUTONOMOUS)).evaluate(proposal(risk=RiskLevel.HIGH))
    assert high.requires_approval and not high.allowed


def test_assisted_flow_pauses_then_resolves():
    app = SentinelOrchestrator(Settings(AutonomyMode.ASSISTED), reasoner=deterministic_reasoner())
    incident = create_demo_incident(app, "bad_deployment", "checkout")
    app.respond(incident.id)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.pending_actions[0]["tool"] == "rollback_deployment"
    app.approve(incident.id)
    assert incident.status is IncidentStatus.RESOLVED
    assert app.events.verify(incident.id)


def test_autonomous_capacity_recovery():
    app = SentinelOrchestrator(Settings(AutonomyMode.AUTONOMOUS, 0.8), reasoner=deterministic_reasoner())
    incident = create_demo_incident(app, "capacity", "payments")
    app.respond(incident.id)
    assert incident.status is IncidentStatus.RESOLVED
    assert incident.executed_actions[0]["action"]["tool"] == "scale_service"


def test_hash_chain_and_runbook_retrieval():
    store = EventStore()
    first = store.append("i-1", "created", {"x": 1})
    second = store.append("i-1", "investigated", {"x": 2})
    assert second.previous_hash == first.hash and store.verify("i-1")
    assert RunbookRetriever().search("errors after deployment rollback", "checkout")[0].id == "RB-DEPLOY-001"


def test_diagnostics_are_ready(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SENTINELOPS_AGENT_BACKEND", "auto")
    report = run_diagnostics()
    assert report["ok"]
    assert all(check["ok"] for check in report["checks"].values())
    assert "rollback_deployment" in report["checks"]["tool_registry"]["detail"]
    assert report["checks"]["llm"]["detail"]["active_backend"] == "deterministic"


def test_api_assisted_incident_flow(monkeypatch):
    control_plane = SentinelOrchestrator(Settings(AutonomyMode.ASSISTED), reasoner=deterministic_reasoner())
    monkeypatch.setattr(api_module, "control_plane", control_plane)

    assert client.get("/health").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["ok"]

    fault = client.post("/api/simulator/faults", json={"service": "checkout", "fault": "bad_deployment"})
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

    approved = client.post(f"/api/incidents/{incident_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "resolved"

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
        config = LLMConfig(backend="openai", model="gpt-test", fallback_to_deterministic=True)

        def __init__(self):
            self.calls = []

        def status(self):
            return {"requested_backend": "openai", "active_backend": "openai", "model": "gpt-test", "configured": True}

        def generate_json(self, *, schema_name, instructions, payload, schema):
            self.calls.append(schema_name)
            if schema_name == "sentinelops_incident_findings":
                data = {
                    "findings": [
                        {"kind": "capacity_pressure", "detail": "telemetry and queue logs indicate saturation", "confidence": 0.94},
                        {"kind": "latency_spike", "detail": "p95 latency is well above the SLO", "confidence": 0.90},
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
            return LLMResult(data=data, model="gpt-test", response_id=f"resp_{len(self.calls)}", latency_ms=12.0)

    reasoner = ScriptedReasoner()
    app = SentinelOrchestrator(Settings(AutonomyMode.AUTONOMOUS, 0.8), reasoner=reasoner)
    incident = create_demo_incident(app, "capacity", "payments")
    app.respond(incident.id)

    assert reasoner.calls == ["sentinelops_incident_findings", "sentinelops_remediation_plan"]
    assert incident.status is IncidentStatus.RESOLVED
    assert incident.executed_actions[0]["action"]["tool"] == "scale_service"
    events = app.trace(incident.id)["events"]
    assert any(event["event_type"] == "planner.llm_completed" for event in events)
    investigator = next(event for event in events if event["event_type"] == "investigator.completed")
    assert investigator["payload"]["reasoning"]["backend"] == "openai"


def test_eval_gate():
    report = run_evals()
    assert report["backend"] == "deterministic"
    assert report["resolution_rate"] == 1.0
    assert report["tool_selection_accuracy"] == 1.0
    assert report["unsafe_action_rate"] == 0.0
    assert report["trace_integrity_rate"] == 1.0
