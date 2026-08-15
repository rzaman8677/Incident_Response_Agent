from __future__ import annotations

import argparse
import json
import os

from .config import settings_from_env
from .core import AutonomyMode
from .diagnostics import run_diagnostics
from .evals import run_evals
from .llm import LLMConfig, LLMError, OpenAIReasoner
from .orchestrator import SentinelOrchestrator, create_demo_incident


def _add_llm_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=["auto", "openai", "deterministic"], default=None)
    parser.add_argument("--model", default=None)


def _apply_llm_flags(args: argparse.Namespace) -> None:
    backend = getattr(args, "backend", None)
    model = getattr(args, "model", None)
    if backend:
        os.environ["SENTINELOPS_AGENT_BACKEND"] = backend
    if model:
        os.environ["SENTINELOPS_MODEL"] = model


def _forced_openai_preflight() -> tuple[bool, dict]:
    reasoner = OpenAIReasoner(LLMConfig.from_env())
    if reasoner.config.backend == "openai" and not reasoner.enabled:
        return False, reasoner.status()
    return True, reasoner.status()


def main() -> int:
    parser = argparse.ArgumentParser(prog="sentinelops")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo")
    demo.add_argument("--fault", choices=["bad_deployment", "capacity", "crashloop"], default="bad_deployment")
    demo.add_argument("--service", choices=["checkout", "payments", "catalog"], default="checkout")
    demo.add_argument("--mode", choices=[m.value for m in AutonomyMode], default="assisted")
    demo.add_argument("--approve", action="store_true")
    _add_llm_flags(demo)

    sub.add_parser("doctor")
    sub.add_parser("llm-check")
    sub.add_parser("eval")

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    _add_llm_flags(serve)
    args = parser.parse_args()

    if args.command == "doctor":
        report = run_diagnostics()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if args.command == "llm-check":
        reasoner = OpenAIReasoner(LLMConfig.from_env())
        if not reasoner.enabled:
            print(json.dumps({"ok": False, "llm": reasoner.status()}, indent=2))
            return 1
        try:
            result = reasoner.generate_json(
                schema_name="sentinelops_llm_check",
                instructions="Return a successful SentinelOps model connectivity check as strict JSON.",
                payload={"task": "connectivity_check"},
                schema={
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "message": {"type": "string"},
                    },
                    "required": ["ok", "message"],
                    "additionalProperties": False,
                },
            )
            print(json.dumps({"ok": bool(result.data.get("ok")), "model": result.model, "response_id": result.response_id, "latency_ms": round(result.latency_ms, 3), "response": result.data}, indent=2))
            return 0 if result.data.get("ok") else 1
        except LLMError as exc:
            print(json.dumps({"ok": False, "error": str(exc), "llm": reasoner.status()}, indent=2))
            return 1

    if args.command == "eval":
        print(json.dumps(run_evals(), indent=2))
        return 0

    _apply_llm_flags(args)
    ready, llm_status = _forced_openai_preflight()
    if not ready:
        print(json.dumps({"ok": False, "error": "OpenAI backend was requested but is not configured", "llm": llm_status}, indent=2))
        return 1

    if args.command == "serve":
        import uvicorn
        uvicorn.run("sentinelops.api:app", host=args.host, port=args.port)
        return 0

    try:
        app = SentinelOrchestrator(settings_from_env(autonomy_mode=AutonomyMode(args.mode)))
        incident = create_demo_incident(app, args.fault, args.service)
        app.respond(incident.id)
        if incident.pending_actions and args.approve:
            app.approve(incident.id)
    except LLMError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1

    print(json.dumps({"llm": app.reasoner.status(), "policy": {"autonomy_mode": app.settings.autonomy_mode.value, "confidence_threshold": app.settings.autonomous_confidence_threshold, "max_blast_radius": app.settings.max_blast_radius, "action_budget": app.settings.action_budget}, "incident": incident.to_dict(), "trace": app.trace(incident.id)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
