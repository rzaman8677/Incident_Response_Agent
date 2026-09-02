from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from threading import RLock
from time import perf_counter
from typing import Any
import uuid


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class IncidentStatus(str, Enum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AutonomyMode(str, Enum):
    OBSERVE = "observe"
    ASSISTED = "assisted"
    AUTONOMOUS = "autonomous"


@dataclass(slots=True)
class Signal:
    source: str
    metric: str
    value: float
    threshold: float
    service: str
    description: str = ""


@dataclass(slots=True)
class Incident:
    title: str
    service: str
    severity: str
    signals: list[Signal]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: IncidentStatus = IncidentStatus.DETECTED
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    summary: str = ""
    root_cause: str = ""
    confidence: float = 0.0
    runbook_ids: list[str] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    executed_actions: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def touch(self, status: IncidentStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = utcnow()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(slots=True)
class Finding:
    kind: str
    detail: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActionProposal:
    tool: str
    args: dict[str, Any]
    rationale: str
    expected_effect: str
    risk: RiskLevel
    blast_radius: int = 1
    confidence: float = 0.0
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risk"] = self.risk.value
        return data


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: dict[str, Any]
    message: str = ""
    latency_ms: float = 0.0


@dataclass(slots=True)
class Event:
    incident_id: str
    event_type: str
    payload: dict[str, Any]
    sequence: int
    timestamp: str = field(default_factory=utcnow)
    previous_hash: str = ""
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Settings:
    autonomy_mode: AutonomyMode = AutonomyMode.ASSISTED
    autonomous_confidence_threshold: float = 0.82
    max_blast_radius: int = 2
    action_budget: int = 4


class EventStore:
    """Append-only, hash-chained incident stream."""

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._lock = RLock()

    def append(self, incident_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        with self._lock:
            stream = self._events[incident_id]
            previous_hash = stream[-1].hash if stream else "GENESIS"
            sequence = len(stream) + 1
            canonical = self._canonical(incident_id, event_type, payload, sequence, previous_hash)
            event = Event(incident_id=incident_id, event_type=event_type, payload=payload, sequence=sequence, previous_hash=previous_hash, hash=hashlib.sha256(canonical.encode()).hexdigest())
            stream.append(event)
            return event

    def stream(self, incident_id: str) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events.get(incident_id, [])]

    def verify(self, incident_id: str) -> bool:
        previous = "GENESIS"
        for event in self._events.get(incident_id, []):
            canonical = self._canonical(event.incident_id, event.event_type, event.payload, event.sequence, previous)
            expected = hashlib.sha256(canonical.encode()).hexdigest()
            if event.previous_hash != previous or event.hash != expected:
                return False
            previous = event.hash
        return True

    @staticmethod
    def _canonical(incident_id: str, event_type: str, payload: dict[str, Any], sequence: int, previous_hash: str) -> str:
        return json.dumps({"incident_id": incident_id, "event_type": event_type, "payload": payload, "sequence": sequence, "previous_hash": previous_hash}, sort_keys=True, separators=(",", ":"), default=str)


class PolicyEngine:
    WRITE_TOOLS = {"restart_service", "scale_service", "rollback_deployment"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, action: ActionProposal) -> PolicyDecision:
        if action.tool not in self.WRITE_TOOLS:
            return PolicyDecision(True, False, "read-only diagnostic action")
        if self.settings.autonomy_mode is AutonomyMode.OBSERVE:
            return PolicyDecision(False, True, "observe mode forbids state changes")
        if action.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(False, True, f"{action.risk.value}-risk actions always require approval")
        if action.blast_radius > self.settings.max_blast_radius:
            return PolicyDecision(False, True, "blast radius exceeds configured autonomous limit")
        if self.settings.autonomy_mode is AutonomyMode.ASSISTED:
            return PolicyDecision(False, True, "assisted mode requires human approval")
        if action.confidence < self.settings.autonomous_confidence_threshold:
            return PolicyDecision(False, True, "confidence below autonomous threshold")
        return PolicyDecision(True, False, "autonomous policy threshold satisfied")


_TOKEN = re.compile(r"[a-z0-9_\-]+")


@dataclass(slots=True)
class Runbook:
    id: str
    title: str
    services: list[str]
    symptoms: list[str]
    checks: list[str]
    remediation: list[str]
    tags: list[str]


class RunbookRetriever:
    def __init__(self, path: str | Path | None = None) -> None:
        path = Path(path) if path else Path(__file__).with_name("runbooks.json")
        self.runbooks = [Runbook(**item) for item in json.loads(path.read_text())]

    def search(self, query: str, service: str, limit: int = 3) -> list[Runbook]:
        query_tokens = set(_TOKEN.findall(query.lower()))
        scored = []
        for rb in self.runbooks:
            corpus = " ".join([rb.title, *rb.services, *rb.symptoms, *rb.tags]).lower()
            overlap = len(query_tokens & set(_TOKEN.findall(corpus)))
            service_bonus = 4 if service.lower() in {s.lower() for s in rb.services} else 0
            scored.append((overlap + service_bonus, rb))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [rb for score, rb in scored[:limit] if score > 0]


@dataclass(slots=True)
class ServiceState:
    name: str
    replicas: int = 3
    healthy_replicas: int = 3
    error_rate: float = 0.01
    p95_latency_ms: float = 120.0
    cpu_percent: float = 38.0
    deployment: str = "v1.0.0"
    previous_deployment: str = "v0.9.9"
    logs: list[str] = field(default_factory=list)


class CloudSimulator:
    """Deterministic cloud control plane for zero-credential demos/evals."""

    def __init__(self) -> None:
        self.services = {name: ServiceState(name) for name in ("checkout", "payments", "catalog")}
        self._baseline = deepcopy(self.services)

    def reset(self) -> None:
        self.services = deepcopy(self._baseline)

    def inject_fault(self, service: str, fault: str) -> dict[str, Any]:
        s = self._service(service)
        if fault == "bad_deployment":
            s.previous_deployment = s.deployment
            s.deployment = "v1.1.0-bad"
            s.error_rate, s.p95_latency_ms = 0.34, 1850
            s.logs += ["ERROR NullPointerException after deploy v1.1.0-bad", "WARN downstream retries exhausted"]
        elif fault == "capacity":
            s.cpu_percent, s.p95_latency_ms, s.error_rate = 97, 2400, 0.12
            s.logs.append("WARN worker pool saturated: queue_depth=1842")
        elif fault == "crashloop":
            s.healthy_replicas = max(0, s.replicas - 2)
            s.error_rate = 0.41
            s.logs.append("ERROR process exited code=137; restarting container")
        else:
            raise ValueError(f"unknown fault: {fault}")
        return self.snapshot(service)

    def snapshot(self, service: str) -> dict[str, Any]:
        return asdict(self._service(service))

    def all_state(self) -> dict[str, Any]:
        return {name: asdict(state) for name, state in self.services.items()}

    def service_health(self, service: str) -> dict[str, Any]:
        s = self._service(service)
        healthy = s.error_rate < 0.05 and s.p95_latency_ms < 500 and s.healthy_replicas == s.replicas
        return {"service": service, "healthy": healthy, **asdict(s)}

    def metrics(self, service: str) -> dict[str, Any]:
        s = self._service(service)
        return {"service": service, "error_rate": s.error_rate, "p95_latency_ms": s.p95_latency_ms, "cpu_percent": s.cpu_percent, "replicas": s.replicas, "healthy_replicas": s.healthy_replicas}

    def query_logs(self, service: str, contains: str = "") -> dict[str, Any]:
        logs = self._service(service).logs[-50:]
        if contains:
            logs = [line for line in logs if contains.lower() in line.lower()]
        return {"service": service, "logs": logs}

    def restart_service(self, service: str) -> dict[str, Any]:
        s = self._service(service)
        s.healthy_replicas = s.replicas
        if s.deployment.endswith("-bad"):
            s.error_rate, s.p95_latency_ms = 0.22, 1100
            s.logs.append("INFO restarted pods; errors persist on current deployment")
        else:
            s.error_rate, s.p95_latency_ms = 0.01, 130
            s.logs.append("INFO rolling restart completed")
        return self.service_health(service)

    def scale_service(self, service: str, replicas: int) -> dict[str, Any]:
        if not 1 <= replicas <= 20:
            raise ValueError("replicas must be between 1 and 20")
        s = self._service(service)
        old = s.replicas
        s.replicas = s.healthy_replicas = replicas
        if s.cpu_percent > 85 and replicas > old:
            ratio = old / replicas
            s.cpu_percent = max(35, s.cpu_percent * ratio)
            s.p95_latency_ms = max(140, 180 + (s.p95_latency_ms - 180) * ratio**3)
            s.error_rate = min(s.error_rate, 0.02)
        s.logs.append(f"INFO scaled from {old} to {replicas} replicas")
        return self.service_health(service)

    def rollback_deployment(self, service: str) -> dict[str, Any]:
        s = self._service(service)
        current = s.deployment
        s.deployment, s.previous_deployment = s.previous_deployment, current
        s.error_rate, s.p95_latency_ms, s.cpu_percent = 0.01, 125, 40
        s.healthy_replicas = s.replicas
        s.logs.append(f"INFO rolled back {current} -> {s.deployment}")
        return self.service_health(service)

    def _service(self, service: str) -> ServiceState:
        if service not in self.services:
            raise ValueError(f"unknown service: {service}")
        return self.services[service]


ToolFn = Callable[..., dict[str, Any]]


class ToolRegistry:
    def __init__(self, simulator: CloudSimulator) -> None:
        self._tools: dict[str, ToolFn] = {"get_service_health": simulator.service_health, "get_metrics": simulator.metrics, "query_logs": simulator.query_logs, "restart_service": simulator.restart_service, "scale_service": simulator.scale_service, "rollback_deployment": simulator.rollback_deployment}

    def names(self) -> list[str]:
        return sorted(self._tools)

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        fn = self._tools.get(name)
        if fn is None:
            return ToolResult(False, {}, f"unknown tool: {name}")
        start = perf_counter()
        try:
            return ToolResult(True, fn(**args), latency_ms=(perf_counter() - start) * 1000)
        except Exception as exc:
            return ToolResult(False, {}, str(exc), latency_ms=(perf_counter() - start) * 1000)
