from __future__ import annotations

from dataclasses import asdict, dataclass

from .core import ActionProposal, EventStore, Finding, Incident, RiskLevel, Runbook, ToolRegistry, ToolResult


@dataclass(slots=True)
class AgentContext:
    tools: ToolRegistry
    events: EventStore


class Agent:
    name = "agent"

    def __init__(self, context: AgentContext) -> None:
        self.context = context

    def emit(self, incident_id: str, event_type: str, payload: dict) -> None:
        self.context.events.append(incident_id, f"{self.name}.{event_type}", payload)


class InvestigatorAgent(Agent):
    name = "investigator"

    def investigate(self, incident: Incident, runbooks: list[Runbook]) -> list[Finding]:
        metrics = self.context.tools.execute("get_metrics", {"service": incident.service})
        logs = self.context.tools.execute("query_logs", {"service": incident.service})
        health = self.context.tools.execute("get_service_health", {"service": incident.service})
        findings: list[Finding] = []
        if metrics.ok:
            m = metrics.output
            if m["error_rate"] >= 0.10:
                findings.append(Finding("error_spike", f"error rate is {m['error_rate']:.0%}", 0.95, m))
            if m["p95_latency_ms"] >= 1000:
                findings.append(Finding("latency_spike", f"p95 latency is {m['p95_latency_ms']:.0f}ms", 0.94, m))
            if m["cpu_percent"] >= 85:
                findings.append(Finding("capacity_pressure", f"CPU is {m['cpu_percent']:.0f}%", 0.96, m))
            if m["healthy_replicas"] < m["replicas"]:
                findings.append(Finding("replica_failure", "healthy replicas below desired replicas", 0.97, m))
        if logs.ok:
            joined = " ".join(logs.output.get("logs", [])).lower()
            if "deploy" in joined and ("exception" in joined or "errors persist" in joined):
                findings.append(Finding("bad_deployment", "errors correlate with the current deployment", 0.93, logs.output))
            if "saturated" in joined or "queue_depth" in joined:
                findings.append(Finding("capacity_pressure", "worker pool saturation found in logs", 0.92, logs.output))
            if "code=137" in joined or "restarting container" in joined:
                findings.append(Finding("crashloop", "containers are repeatedly exiting", 0.94, logs.output))
        self.emit(incident.id, "completed", {"health": health.output if health.ok else {}, "findings": [asdict(f) for f in findings], "runbooks": [r.id for r in runbooks]})
        return findings


class PlannerAgent(Agent):
    name = "planner"

    def plan(self, incident: Incident, findings: list[Finding]) -> list[ActionProposal]:
        kinds = {f.kind for f in findings}
        confidence = max((f.confidence for f in findings), default=0.5)
        actions: list[ActionProposal] = []
        if "bad_deployment" in kinds:
            actions.append(ActionProposal("rollback_deployment", {"service": incident.service}, "errors correlate with a bad deployment", "restore known-good release", RiskLevel.MEDIUM, 1, confidence))
        elif "capacity_pressure" in kinds:
            current = self.context.tools.execute("get_metrics", {"service": incident.service})
            replicas = int(current.output.get("replicas", 3)) if current.ok else 3
            actions.append(ActionProposal("scale_service", {"service": incident.service, "replicas": min(replicas * 2, 10)}, "CPU/queue saturation indicates insufficient capacity", "reduce per-replica load", RiskLevel.MEDIUM, 1, confidence))
        elif "replica_failure" in kinds or "crashloop" in kinds:
            actions.append(ActionProposal("restart_service", {"service": incident.service}, "replicas are unhealthy", "restore desired healthy replicas", RiskLevel.MEDIUM, 1, confidence))
        self.emit(incident.id, "created", {"actions": [a.to_dict() for a in actions]})
        return actions


class ReviewerAgent(Agent):
    name = "reviewer"

    def review(self, incident: Incident, actions: list[ActionProposal]) -> list[ActionProposal]:
        accepted: list[ActionProposal] = []
        seen = set()
        for action in actions:
            signature = (action.tool, tuple(sorted((k, str(v)) for k, v in action.args.items())))
            if signature in seen or action.args.get("service") != incident.service:
                continue
            if action.tool == "scale_service" and not 1 <= int(action.args.get("replicas", 0)) <= 10:
                continue
            seen.add(signature)
            accepted.append(action)
        self.emit(incident.id, "completed", {"accepted": [a.to_dict() for a in accepted]})
        return accepted


@dataclass(slots=True)
class ExecutionRecord:
    action: ActionProposal
    result: ToolResult
    deduplicated: bool = False


class RemediatorAgent(Agent):
    name = "remediator"

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        self._ledger: dict[str, ToolResult] = {}

    def execute(self, incident_id: str, action: ActionProposal) -> ExecutionRecord:
        if action.idempotency_key in self._ledger:
            return ExecutionRecord(action, self._ledger[action.idempotency_key], True)
        result = self.context.tools.execute(action.tool, action.args)
        self._ledger[action.idempotency_key] = result
        self.emit(incident_id, "executed", {"action": action.to_dict(), "ok": result.ok, "output": result.output, "latency_ms": round(result.latency_ms, 3), "message": result.message})
        return ExecutionRecord(action, result)


class VerifierAgent(Agent):
    name = "verifier"

    def verify(self, incident: Incident) -> dict:
        health = self.context.tools.execute("get_service_health", {"service": incident.service})
        metrics = self.context.tools.execute("get_metrics", {"service": incident.service})
        result = {"recovered": bool(health.ok and health.output.get("healthy")), "health": health.output if health.ok else {}, "metrics": metrics.output if metrics.ok else {}}
        self.emit(incident.id, "completed", result)
        return result
