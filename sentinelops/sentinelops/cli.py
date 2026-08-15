from __future__ import annotations

import argparse
import json

from .core import AutonomyMode, Settings
from .diagnostics import run_diagnostics
from .evals import run_evals
from .orchestrator import SentinelOrchestrator, create_demo_incident


def main() -> int:
    parser = argparse.ArgumentParser(prog="sentinelops")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--fault", choices=["bad_deployment", "capacity", "crashloop"], default="bad_deployment")
    demo.add_argument("--service", choices=["checkout", "payments", "catalog"], default="checkout")
    demo.add_argument("--mode", choices=[m.value for m in AutonomyMode], default="assisted")
    demo.add_argument("--approve", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("eval")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.command == "doctor":
        report = run_diagnostics()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "eval":
        print(json.dumps(run_evals(), indent=2))
        return 0
    if args.command == "serve":
        import uvicorn
        uvicorn.run("sentinelops.api:app", host=args.host, port=args.port)
        return 0

    app = SentinelOrchestrator(Settings(AutonomyMode(args.mode), 0.80))
    incident = create_demo_incident(app, args.fault, args.service)
    app.respond(incident.id)
    if incident.pending_actions and args.approve:
        app.approve(incident.id)
    print(json.dumps(incident.to_dict(), indent=2))
    print(json.dumps(app.trace(incident.id), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
