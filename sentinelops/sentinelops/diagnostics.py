from __future__ import annotations

import platform
import sys
from typing import Any

from . import __version__
from .core import CloudSimulator, EventStore, RunbookRetriever, ToolRegistry


EXPECTED_TOOLS = {
    "get_service_health",
    "get_metrics",
    "query_logs",
    "restart_service",
    "scale_service",
    "rollback_deployment",
}


def run_diagnostics() -> dict[str, Any]:
    """Run dependency-free readiness checks for local demos and deployments."""
    checks: dict[str, dict[str, Any]] = {}

    python_ok = sys.version_info >= (3, 11)
    checks["python"] = {
        "ok": python_ok,
        "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }

    retriever = RunbookRetriever()
    checks["runbooks"] = {
        "ok": bool(retriever.runbooks),
        "detail": f"{len(retriever.runbooks)} loaded",
    }

    simulator = CloudSimulator()
    tools = ToolRegistry(simulator)
    registered = set(tools.names())
    checks["tool_registry"] = {
        "ok": EXPECTED_TOOLS.issubset(registered),
        "detail": sorted(registered),
    }

    healthy_services = {
        service: simulator.service_health(service)["healthy"]
        for service in simulator.services
    }
    checks["simulator"] = {
        "ok": all(healthy_services.values()),
        "detail": healthy_services,
    }

    store = EventStore()
    store.append("doctor", "diagnostic_started", {"version": __version__})
    store.append("doctor", "diagnostic_finished", {"ok": True})
    checks["event_chain"] = {
        "ok": store.verify("doctor"),
        "detail": "SHA-256 chain verified",
    }

    return {
        "ok": all(check["ok"] for check in checks.values()),
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "checks": checks,
    }
