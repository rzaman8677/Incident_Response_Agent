from __future__ import annotations

from threading import RLock
from typing import Any

from .agents import (
    AgentContext,
    InvestigatorAgent,
    PlannerAgent,
    RemediatorAgent,
    ReviewerAgent,
    VerifierAgent,
)
from .config import settings_from_env
from .core import (
    ActionProposal,
    CloudSimulator,
    EventStore,
    Incident,
    IncidentStatus,
    PolicyEngine,
    RiskLevel,
    RunbookRetriever,
    Settings,
    Signal,
    ToolRegistry,
)
from .llm import LLMError, OpenAIReasoner
from .persistence import state_from_env
from .providers import provider_from_env


class SentinelOrchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        simulator: CloudSimulator | None = None,
        reasoner: OpenAIReasoner | None = None,
        provider: Any | None = None,
        incident_store: Any | None = None,
        event_store: EventStore | None = None,
        execution_ledger: Any | None = None,
    ) -> None:
        self.settings = settings or settings_from_env()
        if simulator is not None and provider is not None:
            raise ValueError("pass either simulator or provider, not both")
        self.infrastructure = provider or simulator or provider_from_env()
        self.simulator = (
            self.infrastructure
            if isinstance(self.infrastructure, CloudSimulator)
            else None
        )
        if incident_store is None or event_store is None or execution_ledger is None:
            default_incidents, default_events, default_ledger = state_from_env()
            incident_store = incident_store or default_incidents
            event_store = event_store or default_events
            execution_ledger = execution_ledger or default_ledger
        self.incident_store = incident_store
        self.events = event_store
        self.execution_ledger = execution_ledger
        self.tools = ToolRegistry(self.infrastructure)
        self.policy = PolicyEngine(self.settings)
        self.runbooks = RunbookRetriever()
        self.reasoner = reasoner or OpenAIReasoner()
        if self.reasoner.config.backend == "openai" and not self.reasoner.enabled:
            raise LLMError(
                self.reasoner.configuration_error
                or "OpenAI backend was requested but is not available"
            )
        context = AgentContext(
            self.tools, self.events, self.reasoner, self.execution_ledger
        )
        self.investigator = InvestigatorAgent(context)
        self.planner = PlannerAgent(context)
        self.reviewer = ReviewerAgent(context)
        self.remediator = RemediatorAgent(context)
        self.verifier = VerifierAgent(context)
        # Kept as a compatibility view for existing simulator callers. Durable
        # backends remain authoritative through incident_store.
        self.incidents: dict[str, Incident] = getattr(self.incident_store, "items", {})
        self._pending: dict[str, list[ActionProposal]] = {}
        self._lock = RLock()

    def create_incident(
        self,
        title: str,
        service: str,
        severity: str = "SEV-2",
        signals: list[Signal] | None = None,
        incident_id: str | None = None,
    ) -> Incident:
        incident = Incident(
            title=title,
            service=service,
            severity=severity,
            signals=signals or [],
            **({"id": incident_id} if incident_id else {}),
        )
        with self._lock:
            self.incident_store.save(incident)
        self.events.append(
            incident.id,
            "incident.created",
            {**incident.to_dict(), "reasoning_backend": self.reasoner.status()},
        )
        return incident

    def respond(self, incident_id: str, approved: bool = False) -> Incident:
        incident = self.get(incident_id)
        if incident.status is IncidentStatus.AWAITING_APPROVAL:
            if not approved:
                return incident
            actions = self._pending.pop(incident.id, None)
            if actions is None:
                actions = [
                    self._proposal_from_dict(item) for item in incident.pending_actions
                ]
            self.events.append(
                incident.id,
                "human.approved",
                {"actions": [a.to_dict() for a in actions]},
            )
            return self._execute_and_verify(incident, actions)

        incident.touch(IncidentStatus.INVESTIGATING)
        self.incident_store.save(incident)
        self.events.append(
            incident.id, "orchestrator.state", {"status": incident.status.value}
        )
        query = " ".join(
            [incident.title, *(signal.description for signal in incident.signals)]
        )
        runbooks = self.runbooks.search(query, incident.service)
        incident.runbook_ids = [rb.id for rb in runbooks]
        findings = self.investigator.investigate(incident, runbooks)
        incident.root_cause = self._root_cause(findings)
        incident.confidence = max((f.confidence for f in findings), default=0.2)
        incident.summary = f"{incident.service} incident: " + (
            "; ".join(f.detail for f in findings[:3]) or "no conclusive diagnostics"
        )
        incident.touch(IncidentStatus.PLANNING)

        actions = self.reviewer.review(incident, self.planner.plan(incident, findings))
        if not actions:
            incident.touch(IncidentStatus.ESCALATED)
            self.events.append(
                incident.id,
                "orchestrator.escalated",
                {"reason": "no safe remediation plan"},
            )
            self.incident_store.save(incident)
            return incident

        executable: list[ActionProposal] = []
        gated: list[ActionProposal] = []
        for action in actions[: self.settings.action_budget]:
            # The model may describe risk/confidence, but it cannot weaken the
            # deterministic safety floor. All current write tools affect one
            # service and are at least medium risk; confidence cannot exceed
            # the grounded incident evidence confidence.
            if action.risk is RiskLevel.LOW:
                action.risk = RiskLevel.MEDIUM
            action.blast_radius = max(1, action.blast_radius)
            action.confidence = min(action.confidence, incident.confidence)

            decision = self.policy.evaluate(action)
            self.events.append(
                incident.id,
                "policy.decision",
                {
                    "action": action.to_dict(),
                    "allowed": decision.allowed,
                    "requires_approval": decision.requires_approval,
                    "reason": decision.reason,
                },
            )
            if decision.allowed:
                executable.append(action)
            elif decision.requires_approval:
                gated.append(action)

        if gated and not approved:
            self._pending[incident.id] = gated
            incident.pending_actions = [a.to_dict() for a in gated]
            incident.touch(IncidentStatus.AWAITING_APPROVAL)
            self.events.append(
                incident.id,
                "orchestrator.awaiting_approval",
                {"actions": incident.pending_actions},
            )
            self.incident_store.save(incident)
            return incident
        if approved:
            executable.extend(gated)
        return self._execute_and_verify(incident, executable)

    def approve(self, incident_id: str) -> Incident:
        return self.respond(incident_id, approved=True)

    def get(self, incident_id: str) -> Incident:
        incident = self.incident_store.get(incident_id)
        if incident is None:
            raise KeyError(f"incident not found: {incident_id}")
        return incident

    def list(self) -> list[Incident]:
        return self.incident_store.list()

    def trace(self, incident_id: str) -> dict[str, Any]:
        self.get(incident_id)
        return {
            "incident_id": incident_id,
            "verified": self.events.verify(incident_id),
            "events": self.events.stream(incident_id),
        }

    def _execute_and_verify(
        self, incident: Incident, actions: list[ActionProposal]
    ) -> Incident:
        incident.pending_actions = []
        incident.touch(IncidentStatus.EXECUTING)
        self.incident_store.save(incident)
        self.events.append(
            incident.id, "orchestrator.state", {"status": incident.status.value}
        )
        for action in actions:
            record = self.remediator.execute(incident.id, action)
            incident.executed_actions.append(
                {
                    "action": action.to_dict(),
                    "ok": record.result.ok,
                    "output": record.result.output,
                    "deduplicated": record.deduplicated,
                }
            )
            if not record.result.ok:
                incident.touch(IncidentStatus.ESCALATED)
                self.incident_store.save(incident)
                return incident
        incident.touch(IncidentStatus.VERIFYING)
        incident.verification = self.verifier.verify(incident)
        incident.touch(
            IncidentStatus.RESOLVED
            if incident.verification["recovered"]
            else IncidentStatus.ESCALATED
        )
        self.events.append(
            incident.id,
            "incident.resolution",
            {"status": incident.status.value, "verification": incident.verification},
        )
        self.incident_store.save(incident)
        return incident

    @staticmethod
    def _proposal_from_dict(data: dict[str, Any]) -> ActionProposal:
        return ActionProposal(
            tool=data["tool"],
            args=dict(data["args"]),
            rationale=data.get("rationale", "approved remediation"),
            expected_effect=data.get("expected_effect", "restore service health"),
            risk=RiskLevel(data.get("risk", RiskLevel.MEDIUM.value)),
            blast_radius=int(data.get("blast_radius", 1)),
            confidence=float(data.get("confidence", 0.0)),
            idempotency_key=data["idempotency_key"],
        )

    @staticmethod
    def _root_cause(findings) -> str:
        kinds = {f.kind for f in findings}
        for kind in (
            "bad_deployment",
            "capacity_pressure",
            "crashloop",
            "replica_failure",
            "error_spike",
            "latency_spike",
        ):
            if kind in kinds:
                return kind
        return "unknown"


def build_signal(
    service: str,
    metric: str,
    value: float,
    threshold: float,
    description: str = "",
    source: str = "simulator",
) -> Signal:
    return Signal(source, metric, value, threshold, service, description)


def create_demo_incident(
    app: SentinelOrchestrator, fault: str = "bad_deployment", service: str = "checkout"
) -> Incident:
    if app.simulator is None:
        raise RuntimeError(
            "fault injection is available only with SENTINELOPS_INFRA_BACKEND=simulator"
        )
    app.simulator.inject_fault(service, fault)
    description = {
        "bad_deployment": "error spike started immediately after the latest deployment",
        "capacity": "latency and CPU saturated during a traffic burst",
        "crashloop": "replicas are repeatedly restarting",
    }[fault]
    metrics = app.simulator.metrics(service)
    return app.create_incident(
        title=f"{service} production SLO violation",
        service=service,
        signals=[
            build_signal(
                service, "error_rate", metrics["error_rate"], 0.05, description
            )
        ],
    )
