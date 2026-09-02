from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .diagnostics import run_diagnostics
from .evals import run_evals
from .orchestrator import SentinelOrchestrator, build_signal

app = FastAPI(title="SentinelOps", version="0.2.0", description="Policy-gated multi-agent incident response control plane with OpenAI reasoning")
control_plane = SentinelOrchestrator()
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class IncidentCreate(BaseModel):
    title: str
    service: str = Field(pattern="^(checkout|payments|catalog)$")
    severity: str = "SEV-2"
    metric: str = "error_rate"
    value: float = 0.2
    threshold: float = 0.05
    description: str = "production SLO violation"


class FaultCreate(BaseModel):
    service: str = Field(pattern="^(checkout|payments|catalog)$")
    fault: str = Field(pattern="^(bad_deployment|capacity|crashloop)$")


@app.get("/")
def dashboard():
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "sentinelops", "tools": control_plane.tools.names(), "llm": control_plane.reasoner.status()}


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
    verified = sum(1 for incident in incidents if control_plane.events.verify(incident.id))
    return {
        "incidents_total": len(incidents),
        "incidents_by_status": dict(sorted(statuses.items())),
        "verified_traces": verified,
        "trace_integrity_rate": verified / len(incidents) if incidents else 1.0,
        "registered_tools": len(control_plane.tools.names()),
        "simulated_services": len(control_plane.simulator.services),
        "llm": control_plane.reasoner.status(),
    }


@app.get("/api/incidents")
def list_incidents():
    return [incident.to_dict() for incident in control_plane.list()]


@app.post("/api/incidents")
def create_incident(body: IncidentCreate):
    incident = control_plane.create_incident(body.title, body.service, body.severity, [build_signal(body.service, body.metric, body.value, body.threshold, body.description)])
    return incident.to_dict()


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
    return control_plane.simulator.inject_fault(body.service, body.fault)


@app.get("/api/simulator/state")
def simulator_state():
    return control_plane.simulator.all_state()


@app.get("/api/evals")
def evals():
    return run_evals()
