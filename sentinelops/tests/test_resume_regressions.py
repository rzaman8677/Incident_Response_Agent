from concurrent.futures import ThreadPoolExecutor

from test_integrations import (
    FakeFirestoreClient,
    deterministic_reasoner,
    firestore_state,
)

from sentinelops.core import AutonomyMode, IncidentStatus, Settings
from sentinelops.orchestrator import SentinelOrchestrator, create_demo_incident


def orchestrator(**kwargs):
    return SentinelOrchestrator(reasoner=deterministic_reasoner(), **kwargs)


def test_duplicate_create_preserves_resolved_incident_and_trace():
    app = orchestrator(settings=Settings(AutonomyMode.AUTONOMOUS))
    incident = create_demo_incident(app)
    app.respond(incident.id)
    before = app.trace(incident.id)
    duplicate = app.create_incident("redelivery", "payments", incident_id=incident.id)
    assert duplicate.status is IncidentStatus.RESOLVED
    assert duplicate.service == "checkout"
    assert app.trace(incident.id) == before


def test_concurrent_responders_plan_once():
    app = orchestrator()
    incident = create_demo_incident(app)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: app.respond(incident.id), range(24)))
    events = app.trace(incident.id)["events"]
    assert (
        sum(event["event_type"] == "orchestrator.awaiting_approval" for event in events)
        == 1
    )
    assert not incident.executed_actions
    assert app.trace(incident.id)["verified"]


def test_approval_executes_persisted_plan_even_if_cache_changes():
    app = orchestrator()
    incident = create_demo_incident(app)
    app.respond(incident.id)
    app._pending[incident.id][0].args["service"] = "payments"
    result = app.approve(
        incident.id, approver="operator", plan_hash=incident.pending_plan_hash
    )
    assert result.status is IncidentStatus.RESOLVED
    assert result.executed_actions[0]["action"]["args"]["service"] == "checkout"


def test_empty_execution_does_not_report_recovery():
    app = orchestrator()
    incident = app.create_incident("denied plan", "checkout")
    assert app._execute_and_verify(incident, []).status is IncidentStatus.ESCALATED
    assert not incident.executed_actions


def test_firestore_creation_and_lifecycle_claims_are_conditional():
    client = FakeFirestoreClient()
    incidents, events, ledger = firestore_state(client)
    first = orchestrator(
        incident_store=incidents, event_store=events, execution_ledger=ledger
    )
    incident = create_demo_incident(first)
    second = orchestrator(
        incident_store=incidents, event_store=events, execution_ledger=ledger
    )
    assert (
        second.create_incident("duplicate", "payments", incident_id=incident.id).title
        == incident.title
    )
    assert incidents.transition(
        incident.id, IncidentStatus.DETECTED, IncidentStatus.INVESTIGATING
    )
    assert (
        incidents.transition(
            incident.id, IncidentStatus.DETECTED, IncidentStatus.INVESTIGATING
        )
        is None
    )
    assert len(events.stream(incident.id)) == 1


def test_processing_exception_is_escalated_instead_of_stuck(monkeypatch):
    app = orchestrator()
    incident = create_demo_incident(app)

    def fail(*args):
        raise TimeoutError("mock diagnostic timeout")

    monkeypatch.setattr(app.investigator, "investigate", fail)
    result = app.respond(incident.id)
    assert result.status is IncidentStatus.ESCALATED
    assert not result.executed_actions
    assert app.trace(incident.id)["verified"]


def test_policy_rejects_nonfinite_confidence_and_unknown_tools():
    from test_system import proposal

    from sentinelops.core import PolicyEngine

    engine = PolicyEngine(Settings(AutonomyMode.AUTONOMOUS))
    for confidence in [float("nan"), float("inf"), -1.0]:
        decision = engine.evaluate(proposal(confidence=confidence))
        assert not decision.allowed and not decision.requires_approval
    assert not engine.evaluate(proposal(tool="delete_database")).allowed


def test_dashboard_is_available_from_the_package():
    from fastapi.testclient import TestClient

    from sentinelops.api import app

    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "SentinelOps" in response.text
