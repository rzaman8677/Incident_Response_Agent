from fastapi.testclient import TestClient

import sentinelops.api as api_module
from sentinelops.core import ActionProposal, AutonomyMode, EventStore, IncidentStatus, PolicyEngine, RiskLevel, RunbookRetriever, Settings
from sentinelops.diagnostics import run_diagnostics
from sentinelops.evals import run_evals
from sentinelops.orchestrator import SentinelOrchestrator, create_demo_incident


client = TestClient(api_module.app)


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
    app = SentinelOrchestrator(Settings(AutonomyMode.ASSISTED))
    incident = create_demo_incident(app, "bad_deployment", "checkout")
    app.respond(incident.id)
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.pending_actions[0]["tool"] == "rollback_deployment"
    app.approve(incident.id)
    assert incident.status is IncidentStatus.RESOLVED
    assert app.events.verify(incident.id)


def test_autonomous_capacity_recovery():
    app = SentinelOrchestrator(Settings(AutonomyMode.AUTONOMOUS, 0.8))
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


def test_diagnostics_are_ready():
    report = run_diagnostics()
    assert report["ok"]
    assert all(check["ok"] for check in report["checks"].values())
    assert "rollback_deployment" in report["checks"]["tool_registry"]["detail"]


def test_api_assisted_incident_flow(monkeypatch):
    control_plane = SentinelOrchestrator(Settings(AutonomyMode.ASSISTED))
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


def test_eval_gate():
    report = run_evals()
    assert report["resolution_rate"] == 1.0
    assert report["tool_selection_accuracy"] == 1.0
    assert report["unsafe_action_rate"] == 0.0
    assert report["trace_integrity_rate"] == 1.0
