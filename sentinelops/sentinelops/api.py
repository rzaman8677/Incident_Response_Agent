from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .diagnostics import run_diagnostics
from .evals import run_evals
from .orchestrator import SentinelOrchestrator, build_signal

app = FastAPI(
    title="SentinelOps",
    version="0.2.0",
    description="Policy-gated multi-agent incident response control plane with OpenAI reasoning",
)
control_plane = SentinelOrchestrator()
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class IncidentCreate(BaseModel):
    title: str
    service: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    severity: str = "SEV-2"
    metric: str = "error_rate"
    value: float = 0.2
    threshold: float = 0.05
    description: str = "production SLO violation"


class FaultCreate(BaseModel):
    service: str = Field(pattern="^(checkout|payments|catalog)$")
    fault: str = Field(pattern="^(bad_deployment|capacity|crashloop)$")


class PubSubMessage(BaseModel):
    data: str
    messageId: str | None = None
    message_id: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)


class PubSubEnvelope(BaseModel):
    message: PubSubMessage
    subscription: str | None = None


@app.get("/")
def dashboard():
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "sentinelops",
        "infrastructure_backend": getattr(
            control_plane.infrastructure, "backend_name", "simulator"
        ),
        "state_backend": getattr(
            control_plane.incident_store, "backend_name", "memory"
        ),
        "tools": control_plane.tools.names(),
        "llm": control_plane.reasoner.status(),
    }


@app.get("/ready")
def ready(response: Response):
    report = run_diagnostics()
    if not report["ok"]:
        response.status_code = 503
    return report


@app.get("/api/metrics")
def operational_metrics():
    incidents = control_plane.list()
    statuses = Counter(incident.status.value for incident in incidents)
    verified = sum(
        1 for incident in incidents if control_plane.events.verify(incident.id)
    )
    return {
        "incidents_total": len(incidents),
        "incidents_by_status": dict(sorted(statuses.items())),
        "verified_traces": verified,
        "trace_integrity_rate": verified / len(incidents) if incidents else 1.0,
        "registered_tools": len(control_plane.tools.names()),
        "simulated_services": len(control_plane.simulator.services)
        if control_plane.simulator
        else 0,
        "infrastructure_backend": getattr(
            control_plane.infrastructure, "backend_name", "simulator"
        ),
        "state_backend": getattr(
            control_plane.incident_store, "backend_name", "memory"
        ),
        "llm": control_plane.reasoner.status(),
    }


@app.get("/api/incidents")
def list_incidents():
    return [incident.to_dict() for incident in control_plane.list()]


@app.post("/api/incidents")
def create_incident(body: IncidentCreate):
    source = getattr(control_plane.infrastructure, "backend_name", "api")
    incident = control_plane.create_incident(
        body.title,
        body.service,
        body.severity,
        [
            build_signal(
                body.service,
                body.metric,
                body.value,
                body.threshold,
                body.description,
                source,
            )
        ],
    )
    return incident.to_dict()


@app.post("/api/events/pubsub")
def receive_pubsub(envelope: PubSubEnvelope):
    """Receive a Cloud Pub/Sub push message containing an IncidentCreate payload."""

    try:
        decoded = base64.b64decode(envelope.message.data, validate=True)
        payload: dict[str, Any] = json.loads(decoded)
        body = IncidentCreate.model_validate(payload)
    except Exception as exc:
        raise HTTPException(400, f"invalid Pub/Sub incident payload: {exc}") from exc

    message_id = envelope.message.messageId or envelope.message.message_id
    stable_source = message_id or hashlib.sha256(decoded).hexdigest()
    incident_id = "pubsub-" + hashlib.sha256(stable_source.encode()).hexdigest()[:32]
    existing = control_plane.incident_store.get(incident_id)
    duplicate = existing is not None
    if existing is None:
        existing = control_plane.create_incident(
            body.title,
            body.service,
            body.severity,
            [
                build_signal(
                    body.service,
                    body.metric,
                    body.value,
                    body.threshold,
                    body.description,
                    "pubsub",
                )
            ],
            incident_id=incident_id,
        )
    if payload.get("respond", True) and existing.status.value == "detected":
        existing = control_plane.respond(existing.id)
    return {"accepted": True, "duplicate": duplicate, "incident": existing.to_dict()}


@app.post("/api/incidents/{incident_id}/respond")
def respond(incident_id: str):
    try:
        return control_plane.respond(incident_id).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/incidents/{incident_id}/approve")
def approve(incident_id: str):
    try:
        return control_plane.approve(incident_id).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/incidents/{incident_id}/trace")
def trace(incident_id: str):
    try:
        return control_plane.trace(incident_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/simulator/faults")
def inject_fault(body: FaultCreate):
    if control_plane.simulator is None:
        raise HTTPException(
            409, "fault injection requires SENTINELOPS_INFRA_BACKEND=simulator"
        )
    return control_plane.simulator.inject_fault(body.service, body.fault)


@app.get("/api/simulator/state")
def simulator_state():
    if control_plane.simulator is None:
        raise HTTPException(
            409, "simulator state requires SENTINELOPS_INFRA_BACKEND=simulator"
        )
    return control_plane.simulator.all_state()


@app.get("/api/evals")
def evals():
    return run_evals()
