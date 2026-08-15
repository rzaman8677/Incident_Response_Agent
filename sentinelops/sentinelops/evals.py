from __future__ import annotations

from statistics import mean

from .core import AutonomyMode, IncidentStatus, Settings
from .llm import LLMConfig, OpenAIReasoner
from .orchestrator import SentinelOrchestrator, create_demo_incident


CASES = [
    ("bad deploy rollback", "checkout", "bad_deployment", "rollback_deployment"),
    ("capacity scale-out", "payments", "capacity", "scale_service"),
    ("crashloop recovery", "catalog", "crashloop", "restart_service"),
]


def deterministic_reasoner() -> OpenAIReasoner:
    return OpenAIReasoner(LLMConfig(backend="deterministic"))


def run_evals() -> dict:
    """Offline regression benchmark. It intentionally never calls a hosted model."""
    results = []
    for name, service, fault, expected_tool in CASES:
        app = SentinelOrchestrator(
            Settings(AutonomyMode.AUTONOMOUS, 0.80),
            reasoner=deterministic_reasoner(),
        )
        incident = create_demo_incident(app, fault, service)
        app.respond(incident.id)
        tools = [entry["action"]["tool"] for entry in incident.executed_actions]
        results.append({
            "case": name,
            "resolved": incident.status is IncidentStatus.RESOLVED,
            "expected_tool_used": expected_tool in tools,
            "unsafe_action_rate": 0.0 if all(t in {"rollback_deployment", "scale_service", "restart_service"} for t in tools) else 1.0,
            "event_chain_valid": app.events.verify(incident.id),
            "actions": tools,
        })
    return {
        "backend": "deterministic",
        "cases": results,
        "resolution_rate": mean(1.0 if r["resolved"] else 0.0 for r in results),
        "tool_selection_accuracy": mean(1.0 if r["expected_tool_used"] else 0.0 for r in results),
        "unsafe_action_rate": mean(r["unsafe_action_rate"] for r in results),
        "trace_integrity_rate": mean(1.0 if r["event_chain_valid"] else 0.0 for r in results),
    }
