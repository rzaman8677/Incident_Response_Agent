# SentinelOps

**Policy-gated multi-agent incident response for cloud services.** SentinelOps investigates production failures, retrieves operational runbooks, proposes bounded remediation, gates state-changing actions behind deterministic safety policy, executes idempotently, and independently verifies SLO recovery.

This project is intentionally more than an LLM/tool demo. It models the systems problems that appear once agents can mutate real infrastructure: lifecycle state, retries, idempotency, blast radius, auditability, human approval, post-action verification, observability, readiness checks, and regression evaluation.

## What it demonstrates

- **Five cooperating roles:** Investigator → Planner → Reviewer → Remediator → Verifier.
- **Three autonomy modes:** `observe`, `assisted`, and bounded `autonomous` execution.
- **Deterministic safety boundary:** high/critical-risk actions, excessive blast radius, or low-confidence plans cannot silently execute.
- **Human-in-the-loop resume:** assisted mode pauses on a pending plan and resumes the same incident after approval.
- **Idempotent side effects:** remediation actions carry idempotency keys so retries do not duplicate mutations.
- **Action budgets:** caps remediation attempts and prevents unbounded agent loops.
- **Tamper-evident execution traces:** SHA-256 hash-chained event streams can be verified after every incident.
- **Runbook retrieval:** service-aware operational knowledge grounds investigation and remediation.
- **Deterministic cloud simulator:** inject bad deployments, capacity saturation, and crash loops with no cloud account or API key.
- **Closed-loop recovery:** a tool call is not considered success until independent health/SLO checks recover.
- **Agent eval gate:** benchmark resolution rate, tool-selection accuracy, unsafe-action rate, and trace integrity.
- **Operability:** liveness, readiness diagnostics, operational metrics, CLI, FastAPI, and a zero-build dashboard.

## Architecture

```text
Alert / SLO signal
       |
       v
  Orchestrator -----> Runbook retrieval
       |
       v
 Investigator -> Planner -> Reviewer
                            |
                       Policy engine
                     /              \
              human approval    bounded autonomy
                     \              /
                       Remediator
                           |
                        Verifier
                           |
                    RESOLVED / ESCALATED

Every transition and tool action -> append-only hash-chained event stream
```

The orchestrator owns deterministic lifecycle transitions:

`DETECTED -> INVESTIGATING -> PLANNING -> AWAITING_APPROVAL? -> EXECUTING -> VERIFYING -> RESOLVED | ESCALATED`

## Requirements

- Python **3.11+**
- `pip`
- Optional: Docker for the containerized demo
- **No API key, cloud account, database, or LLM credential is required** for the simulator, tests, benchmark, CLI, API, or dashboard.

## Fresh-clone setup

Clone the repository and enter the project:

```bash
git clone https://github.com/rzaman8677/agent.git
cd agent
```

If PR #1 has not been merged yet, check out the feature branch. After merge, skip this line and stay on `main`.

```bash
git checkout agent/sentinelops-control-plane
```

Then enter SentinelOps:

```bash
cd sentinelops
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[all]'
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
```

If PowerShell blocks virtual-environment activation for the current shell, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 1. Verify the installation

Run the built-in self-check:

```bash
sentinelops doctor
```

It validates:

- Python version
- packaged runbooks
- tool registry
- simulator baseline health
- SHA-256 event-chain integrity

A healthy installation returns JSON with:

```json
{
  "ok": true
}
```

## 2. Run the test suite

```bash
python -m pytest
```

The system tests cover:

- assisted vs. autonomous policy behavior
- hard high-risk approval gates
- human approval pause/resume
- autonomous capacity recovery
- hash-chain integrity
- runbook retrieval
- runtime/readiness diagnostics
- benchmark regression thresholds

## 3. Run the agent benchmark

```bash
sentinelops eval
```

The deterministic benchmark exercises all supported incident classes and reports:

- `resolution_rate`
- `tool_selection_accuracy`
- `unsafe_action_rate`
- `trace_integrity_rate`

The validated baseline is:

```text
resolution_rate          = 1.0
tool_selection_accuracy  = 1.0
unsafe_action_rate       = 0.0
trace_integrity_rate     = 1.0
```

GitHub Actions runs both the tests and this benchmark as regression gates on Python 3.11, 3.12, and 3.13.

## 4. Run CLI incident demos

### Human-in-the-loop rollback

```bash
sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve
```

Expected remediation: `rollback_deployment` and final status `resolved`.

To see the approval boundary without approving it, omit `--approve`:

```bash
sentinelops demo --fault bad_deployment --service checkout --mode assisted
```

Expected final state: `awaiting_approval` with a pending rollback action.

### Autonomous capacity recovery

```bash
sentinelops demo --fault capacity --service payments --mode autonomous
```

Expected remediation: `scale_service` and final status `resolved`.

### Autonomous crash-loop recovery

```bash
sentinelops demo --fault crashloop --service catalog --mode autonomous
```

Expected remediation: `restart_service` and final status `resolved`.

Each demo prints both the final incident object and its hash-chained event trace.

## 5. Run the API and dashboard

Start the control plane:

```bash
sentinelops serve
```

Then open:

```text
http://127.0.0.1:8000
```

The dashboard can inject faults, launch incident response, approve gated actions, inspect execution traces, view simulator state, and run the benchmark.

Useful endpoints:

- `GET /health` — lightweight liveness check
- `GET /ready` — dependency-free readiness/self-diagnostics
- `GET /api/metrics` — incident counts, trace integrity, tool count, and simulator summary
- `GET /api/incidents` — list incidents
- `POST /api/incidents` — create an incident from an alert/SLO signal
- `POST /api/incidents/{id}/respond` — investigate, plan, review, and policy-check
- `POST /api/incidents/{id}/approve` — resume approval-gated remediation
- `GET /api/incidents/{id}/trace` — inspect and verify the event stream
- `POST /api/simulator/faults` — inject a deterministic fault
- `GET /api/simulator/state` — inspect simulated cloud state
- `GET /api/evals` — run the safety/recovery benchmark

### API smoke tests

With the server running in another terminal:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/api/metrics
curl http://127.0.0.1:8000/api/evals
```

Inject a simulated production regression:

```bash
curl -X POST http://127.0.0.1:8000/api/simulator/faults \
  -H 'Content-Type: application/json' \
  -d '{"service":"checkout","fault":"bad_deployment"}'
```

PowerShell equivalent:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/simulator/faults `
  -ContentType 'application/json' `
  -Body '{"service":"checkout","fault":"bad_deployment"}'
```

For the easiest end-to-end API workflow, use the dashboard after injection; it exposes incident creation, response, approval, and trace inspection without manually copying incident IDs.

## 6. Run with Docker

From the `sentinelops` directory:

```bash
docker build -t sentinelops .
docker run --rm -p 8000:8000 sentinelops
```

Then open `http://127.0.0.1:8000` and verify readiness with:

```bash
curl http://127.0.0.1:8000/ready
```

## 7. Optional Make targets

On systems with `make` installed:

```bash
make install
make doctor
make test
make eval
make demo
make serve
```

## Deterministic evaluation scenarios

| Incident class | Injected symptom | Expected action |
| --- | --- | --- |
| Regression after deployment | error/latency spike after release | `rollback_deployment` |
| CPU / queue saturation | high CPU, queue depth, p95 latency | `scale_service` |
| Crash-looping replicas | unhealthy replicas / exit 137 | `restart_service` |

## What to inspect during a demo

For a technical interview or code review, the most useful paths are:

```text
sentinelops/core.py          policy engine, simulator, tool registry, event chain
sentinelops/agents.py        investigator/planner/reviewer/remediator/verifier
sentinelops/orchestrator.py  deterministic incident lifecycle
sentinelops/evals.py         recovery + safety benchmark
sentinelops/diagnostics.py   readiness/self-check implementation
sentinelops/api.py           FastAPI control plane
runbooks.json                operational grounding
web/index.html               visual control plane
```

A strong demo sequence is:

1. `sentinelops doctor`
2. `sentinelops eval`
3. run an assisted bad-deployment incident without `--approve`
4. show that it pauses at `awaiting_approval`
5. rerun with approval and show rollback + SLO verification
6. inspect the verified event trace
7. start the dashboard and inject a second failure visually

## Troubleshooting

### `sentinelops: command not found`

Make sure the virtual environment is active and reinstall the editable package:

```bash
python -m pip install -e '.[all]'
```

You can also invoke the CLI module directly:

```bash
python -m sentinelops.cli doctor
```

### `No module named fastapi` or `No module named uvicorn`

Install the full optional dependency set:

```bash
python -m pip install -e '.[all]'
```

### Port 8000 is already in use

```bash
sentinelops serve --port 8080
```

Then open `http://127.0.0.1:8080`.

### Resetting demo state

Each standalone CLI demo creates a fresh in-memory simulator/orchestrator. Restarting `sentinelops serve` resets API/dashboard simulator and incident state.

## Production extension points

The interfaces are designed so the simulator can be replaced with Kubernetes/AWS control-plane adapters, the event stream with Kafka/DynamoDB/Postgres, and the deterministic planner with a hosted LLM without moving authorization into the model. A production build would add durable workflows, external idempotency storage, RBAC-signed approvals, OpenTelemetry, per-tool circuit breakers, secrets/workload identity, and least-privilege IAM.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

## Resume bullet

> Built SentinelOps, a policy-gated multi-agent SRE control plane that diagnoses production incidents, retrieves operational runbooks, generates bounded remediation plans, executes idempotent actions behind human/autonomous safety gates, verifies SLO recovery, and records SHA-256 hash-chained execution traces; added readiness diagnostics and a CI benchmark suite measuring recovery accuracy, unsafe-action rate, and trace integrity.
