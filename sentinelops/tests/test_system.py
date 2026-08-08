from sentinelops.core import ActionProposal, AutonomyMode, EventStore, IncidentStatus, PolicyEngine, RiskLevel, RunbookRetriever, Settings
from sentinelops.evals import run_evals
from sentinelops.orchestrator import SentinelOrchestrator, create_demo_incident


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


def test_eval_gate():
    report = run_evals()
    assert report["resolution_rate"] == 1.0
    assert report["tool_selection_accuracy"] == 1.0
    assert report["unsafe_action_rate"] == 0.0
    assert report["trace_integrity_rate"] == 1.0
