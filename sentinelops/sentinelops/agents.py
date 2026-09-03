from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from .core import (
    ActionProposal,
    EventStore,
    Finding,
    Incident,
    RiskLevel,
    Runbook,
    ToolRegistry,
    ToolResult,
)
from .llm import LLMError, OpenAIReasoner

FINDING_KINDS = [
    "bad_deployment",
    "capacity_pressure",
    "crashloop",
    "replica_failure",
    "error_spike",
    "latency_spike",
    "unknown",
]

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": FINDING_KINDS},
                    "detail": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["kind", "detail", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": [
                            "rollback_deployment",
                            "scale_service",
                            "restart_service",
                        ],
                    },
                    "service": {
                        "type": "string",
                        "enum": ["checkout", "payments", "catalog"],
                    },
                    "replicas": {"type": "integer", "minimum": 0, "maximum": 10},
                    "rationale": {"type": "string"},
                    "expected_effect": {"type": "string"},
                    "risk": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                    "blast_radius": {"type": "integer", "minimum": 1, "maximum": 10},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "tool",
                    "service",
                    "replicas",
                    "rationale",
                    "expected_effect",
                    "risk",
                    "blast_radius",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class AgentContext:
    tools: ToolRegistry
    events: EventStore
    reasoner: OpenAIReasoner | None = None
    execution_ledger: Any | None = None


class Agent:
    name = "agent"

    def __init__(self, context: AgentContext) -> None:
        self.context = context

    def emit(self, incident_id: str, event_type: str, payload: dict) -> None:
        self.context.events.append(incident_id, f"{self.name}.{event_type}", payload)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.context.reasoner and self.context.reasoner.enabled)

    @property
    def deterministic_fallback(self) -> bool:
        return (
            not self.context.reasoner
            or self.context.reasoner.config.fallback_to_deterministic
        )


class InvestigatorAgent(Agent):
    name = "investigator"

    def investigate(self, incident: Incident, runbooks: list[Runbook]) -> list[Finding]:
        metrics = self.context.tools.execute(
            "get_metrics", {"service": incident.service}
        )
        logs = self.context.tools.execute("query_logs", {"service": incident.service})
        health = self.context.tools.execute(
            "get_service_health", {"service": incident.service}
        )
        deterministic = self._deterministic_findings(metrics, logs)
        findings = deterministic
        reasoning = {"backend": "deterministic"}

        if self.llm_enabled:
            try:
                result = self.context.reasoner.generate_json(
                    schema_name="sentinelops_incident_findings",
                    instructions=(
                        "You are SentinelOps Investigator. Diagnose a production incident using only the supplied telemetry, logs, signals, and runbooks. "
                        "Do not invent evidence, do not recommend or execute tools, and return only findings supported by the provided observations."
                    ),
                    payload={
                        "incident": incident.to_dict(),
                        "metrics": metrics.output if metrics.ok else {},
                        "logs": logs.output if logs.ok else {},
                        "health": health.output if health.ok else {},
                        "runbooks": [asdict(runbook) for runbook in runbooks],
                    },
                    schema=FINDINGS_SCHEMA,
                )
                findings = self._ground_llm_findings(
                    result.data.get("findings", []), deterministic
                )
                reasoning = {
                    "backend": "openai",
                    "model": result.model,
                    "response_id": result.response_id,
                    "latency_ms": round(result.latency_ms, 3),
                }
            except LLMError as exc:
                self.emit(
                    incident.id,
                    "llm_fallback",
                    {"stage": "investigation", "error": str(exc)},
                )
                if not self.deterministic_fallback:
                    raise

        self.emit(
            incident.id,
            "completed",
            {
                "health": health.output if health.ok else {},
                "findings": [asdict(f) for f in findings],
                "runbooks": [r.id for r in runbooks],
                "reasoning": reasoning,
            },
        )
        return findings

    @staticmethod
    def _deterministic_findings(metrics: ToolResult, logs: ToolResult) -> list[Finding]:
        findings: list[Finding] = []
        if metrics.ok:
            m = metrics.output
            if m["error_rate"] >= 0.10:
                findings.append(
                    Finding(
                        "error_spike", f"error rate is {m['error_rate']:.0%}", 0.95, m
                    )
                )
            if m["p95_latency_ms"] >= 1000:
                findings.append(
                    Finding(
                        "latency_spike",
                        f"p95 latency is {m['p95_latency_ms']:.0f}ms",
                        0.94,
                        m,
                    )
                )
            if m["cpu_percent"] >= 85:
                findings.append(
                    Finding(
                        "capacity_pressure", f"CPU is {m['cpu_percent']:.0f}%", 0.96, m
                    )
                )
            if m["healthy_replicas"] < m["replicas"]:
                findings.append(
                    Finding(
                        "replica_failure",
                        "healthy replicas below desired replicas",
                        0.97,
                        m,
                    )
                )
        if logs.ok:
            joined = " ".join(logs.output.get("logs", [])).lower()
            if "deploy" in joined and (
                "exception" in joined or "errors persist" in joined
            ):
                findings.append(
                    Finding(
                        "bad_deployment",
                        "errors correlate with the current deployment",
                        0.93,
                        logs.output,
                    )
                )
            if "saturated" in joined or "queue_depth" in joined:
                findings.append(
                    Finding(
                        "capacity_pressure",
                        "worker pool saturation found in logs",
                        0.92,
                        logs.output,
                    )
                )
            if "code=137" in joined or "restarting container" in joined:
                findings.append(
                    Finding(
                        "crashloop",
                        "containers are repeatedly exiting",
                        0.94,
                        logs.output,
                    )
                )
        return findings

    @staticmethod
    def _ground_llm_findings(
        raw_findings: list[dict[str, Any]], deterministic: list[Finding]
    ) -> list[Finding]:
        supported = {finding.kind: finding for finding in deterministic}
        if not supported:
            return [
                Finding(
                    str(item["kind"]),
                    str(item["detail"]),
                    min(float(item["confidence"]), 0.70),
                    {
                        "source": "llm",
                        "grounding": "no deterministic signature matched",
                    },
                )
                for item in raw_findings
                if item.get("kind") in FINDING_KINDS
            ]

        merged: list[Finding] = []
        seen: set[str] = set()
        for item in raw_findings:
            kind = str(item.get("kind", ""))
            if kind not in supported or kind in seen:
                continue
            baseline = supported[kind]
            merged.append(
                Finding(
                    kind,
                    str(item.get("detail") or baseline.detail),
                    min(
                        max(float(item.get("confidence", baseline.confidence)), 0.0),
                        baseline.confidence,
                    ),
                    {"source": "openai+telemetry", "observed": baseline.evidence},
                )
            )
            seen.add(kind)
        for baseline in deterministic:
            if baseline.kind not in seen:
                merged.append(baseline)
                seen.add(baseline.kind)
        return merged


class PlannerAgent(Agent):
    name = "planner"

    def plan(self, incident: Incident, findings: list[Finding]) -> list[ActionProposal]:
        if self.llm_enabled:
            try:
                current = self.context.tools.execute(
                    "get_metrics", {"service": incident.service}
                )
                plan_schema = deepcopy(PLAN_SCHEMA)
                plan_schema["properties"]["actions"]["items"]["properties"]["service"][
                    "enum"
                ] = [incident.service]
                result = self.context.reasoner.generate_json(
                    schema_name="sentinelops_remediation_plan",
                    instructions=(
                        "You are SentinelOps Planner. Propose the smallest safe remediation justified by the supplied findings. "
                        "You may only propose rollback_deployment, scale_service, or restart_service. Never claim an action has executed. "
                        "For scale_service set replicas to the desired integer count; for other tools set replicas to 0."
                    ),
                    payload={
                        "incident": incident.to_dict(),
                        "findings": [asdict(finding) for finding in findings],
                        "current_metrics": current.output if current.ok else {},
                        "available_write_tools": [
                            "rollback_deployment",
                            "scale_service",
                            "restart_service",
                        ],
                        "constraints": {
                            "max_scale_replicas": 10,
                            "prefer_single_action": True,
                        },
                    },
                    schema=plan_schema,
                )
                actions = self._actions_from_llm(
                    incident, findings, result.data.get("actions", [])
                )
                self.emit(
                    incident.id,
                    "llm_completed",
                    {
                        "model": result.model,
                        "response_id": result.response_id,
                        "latency_ms": round(result.latency_ms, 3),
                        "accepted_proposals": [action.to_dict() for action in actions],
                    },
                )
                if actions or not self.deterministic_fallback:
                    self.emit(
                        incident.id,
                        "created",
                        {
                            "actions": [a.to_dict() for a in actions],
                            "backend": "openai",
                        },
                    )
                    return actions
            except LLMError as exc:
                self.emit(
                    incident.id,
                    "llm_fallback",
                    {"stage": "planning", "error": str(exc)},
                )
                if not self.deterministic_fallback:
                    raise

        actions = self._deterministic_plan(incident, findings)
        self.emit(
            incident.id,
            "created",
            {"actions": [a.to_dict() for a in actions], "backend": "deterministic"},
        )
        return actions

    def _deterministic_plan(
        self, incident: Incident, findings: list[Finding]
    ) -> list[ActionProposal]:
        kinds = {f.kind for f in findings}
        confidence = max((f.confidence for f in findings), default=0.5)
        actions: list[ActionProposal] = []
        if "bad_deployment" in kinds:
            actions.append(
                ActionProposal(
                    "rollback_deployment",
                    {"service": incident.service},
                    "errors correlate with a bad deployment",
                    "restore known-good release",
                    RiskLevel.MEDIUM,
                    1,
                    confidence,
                )
            )
        elif "capacity_pressure" in kinds:
            current = self.context.tools.execute(
                "get_metrics", {"service": incident.service}
            )
            replicas = int(current.output.get("replicas", 3)) if current.ok else 3
            actions.append(
                ActionProposal(
                    "scale_service",
                    {"service": incident.service, "replicas": min(replicas * 2, 10)},
                    "CPU/queue saturation indicates insufficient capacity",
                    "reduce per-replica load",
                    RiskLevel.MEDIUM,
                    1,
                    confidence,
                )
            )
        elif "replica_failure" in kinds or "crashloop" in kinds:
            actions.append(
                ActionProposal(
                    "restart_service",
                    {"service": incident.service},
                    "replicas are unhealthy",
                    "restore desired healthy replicas",
                    RiskLevel.MEDIUM,
                    1,
                    confidence,
                )
            )
        return actions

    def _actions_from_llm(
        self,
        incident: Incident,
        findings: list[Finding],
        raw_actions: list[dict[str, Any]],
    ) -> list[ActionProposal]:
        kinds = {finding.kind for finding in findings}
        expected_tool = None
        if "bad_deployment" in kinds:
            expected_tool = "rollback_deployment"
        elif "capacity_pressure" in kinds:
            expected_tool = "scale_service"
        elif "replica_failure" in kinds or "crashloop" in kinds:
            expected_tool = "restart_service"

        actions: list[ActionProposal] = []
        for item in raw_actions:
            tool = str(item.get("tool", ""))
            service = str(item.get("service", ""))
            if service != incident.service or tool not in {
                "rollback_deployment",
                "scale_service",
                "restart_service",
            }:
                continue
            if expected_tool and tool != expected_tool:
                continue
            args: dict[str, Any] = {"service": incident.service}
            if tool == "scale_service":
                replicas = int(item.get("replicas", 0))
                if not 1 <= replicas <= 10:
                    continue
                args["replicas"] = replicas
            actions.append(
                ActionProposal(
                    tool=tool,
                    args=args,
                    rationale=str(item.get("rationale", "model-proposed remediation")),
                    expected_effect=str(
                        item.get("expected_effect", "restore service health")
                    ),
                    risk=RiskLevel(str(item.get("risk", "medium"))),
                    blast_radius=int(item.get("blast_radius", 1)),
                    confidence=float(item.get("confidence", 0.5)),
                )
            )
        return actions


class ReviewerAgent(Agent):
    name = "reviewer"

    def review(
        self, incident: Incident, actions: list[ActionProposal]
    ) -> list[ActionProposal]:
        accepted: list[ActionProposal] = []
        seen = set()
        expected = {
            "bad_deployment": "rollback_deployment",
            "capacity_pressure": "scale_service",
            "crashloop": "restart_service",
            "replica_failure": "restart_service",
        }.get(incident.root_cause)
        for action in actions:
            signature = (
                action.tool,
                tuple(sorted((k, str(v)) for k, v in action.args.items())),
            )
            if signature in seen or action.args.get("service") != incident.service:
                continue
            if expected and action.tool != expected:
                continue
            if (
                action.tool == "scale_service"
                and not 1 <= int(action.args.get("replicas", 0)) <= 10
            ):
                continue
            seen.add(signature)
            accepted.append(action)
        self.emit(
            incident.id, "completed", {"accepted": [a.to_dict() for a in accepted]}
        )
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

    def execute(self, incident_id: str, action: ActionProposal) -> ExecutionRecord:
        acquired, previous = self.context.execution_ledger.acquire(
            action.idempotency_key
        )
        if not acquired:
            return ExecutionRecord(
                action,
                previous or ToolResult(False, {}, "action is already executing"),
                True,
            )
        result = self.context.tools.execute(action.tool, action.args)
        self.context.execution_ledger.complete(action.idempotency_key, result)
        self.emit(
            incident_id,
            "executed",
            {
                "action": action.to_dict(),
                "ok": result.ok,
                "output": result.output,
                "latency_ms": round(result.latency_ms, 3),
                "message": result.message,
            },
        )
        return ExecutionRecord(action, result)


class VerifierAgent(Agent):
    name = "verifier"

    def verify(self, incident: Incident) -> dict:
        health = self.context.tools.execute(
            "get_service_health", {"service": incident.service}
        )
        metrics = self.context.tools.execute(
            "get_metrics", {"service": incident.service}
        )
        result = {
            "recovered": bool(health.ok and health.output.get("healthy")),
            "health": health.output if health.ok else {},
            "metrics": metrics.output if metrics.ok else {},
        }
        self.emit(incident.id, "completed", result)
        return result
