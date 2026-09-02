# SentinelOps

**Policy-gated multi-agent incident response for cloud services with real OpenAI reasoning.** SentinelOps uses an LLM-backed Investigator and Planner to reason over telemetry, logs, alerts, and runbooks, then passes every proposed state-changing action through deterministic review, policy, approval, execution, and SLO verification.

The important design choice is that the model **reasons but does not authorize itself**. The OpenAI model can diagnose an incident and propose a remediation, but it cannot directly invoke infrastructure mutations or bypass blast-radius, confidence, risk, action-budget, or human-approval gates.

## What it demonstrates

- **Real OpenAI Responses API reasoning:** Investigator and Planner use strict structured outputs when an API key is configured.
- **Five cooperating roles:** Investigator → Planner → Reviewer → Remediator → Verifier.
- **Three autonomy modes:** `observe`, `assisted`, and bounded `autonomous` execution.
- **Deterministic safety boundary:** high/critical-risk actions, excessive blast radius, unsupported actions, or low-confidence plans cannot silently execute.
- **Grounded LLM investigation:** model findings are cross-checked against observed telemetry signatures before they influence remediation.
- **Constrained LLM planning:** the model can only propose `rollback_deployment`, `scale_service`, or `restart_service`, and proposals are semantically reviewed against the diagnosed root cause.
- **Human-in-the-loop resume:** assisted mode pauses on a pending plan and resumes the same incident after approval.
- **Idempotent side effects:** remediation actions carry idempotency keys so retries do not duplicate mutations.
- **Action budgets:** caps remediation attempts and prevents unbounded agent loops.
- **Tamper-evident execution traces:** SHA-256 hash-chained event streams record model reasoning metadata, policy decisions, approvals, tool execution, and verification.
- **Runbook retrieval:** service-aware operational knowledge grounds investigation and remediation.
- **Deterministic cloud simulator:** inject bad deployments, capacity saturation, and crash loops without a cloud account.
- **Closed-loop recovery:** a tool call is not considered success until independent health/SLO checks recover.
- **Offline agent eval gate:** benchmark resolution rate, tool-selection accuracy, unsafe-action rate, and trace integrity without spending API credits.
- **Operability:** liveness, readiness diagnostics, LLM connectivity check, operational metrics, CLI, FastAPI, Docker, and a zero-build dashboard.

## Architecture

```text
Alert / SLO signal
       |
       v
  Orchestrator ------> Runbook retrieval
       |
       v
OpenAI Investigator
  telemetry + logs + runbooks
       |
       v
 OpenAI Planner
       |
       v
Deterministic Reviewer
       |
       v
Deterministic Policy Engine
     /                  \
human approval      bounded autonomy
     \                  /
       v
   Remediator
       |
       v
Independent Verifier
       |
RESOLVED / ESCALATED

Every transition, LLM stage, policy decision, approval, and tool action
-> append-only SHA-256 hash-chained event stream
```

The orchestrator owns deterministic lifecycle transitions:

`DETECTED -> INVESTIGATING -> PLANNING -> AWAITING_APPROVAL? -> EXECUTING -> VERIFYING -> RESOLVED | ESCALATED`

## LLM modes

SentinelOps supports three reasoning backends:

| Backend | Behavior |
| --- | --- |
| `auto` | Uses OpenAI when `OPENAI_API_KEY` is configured; otherwise falls back to deterministic reasoning. This is the default. |
| `openai` | Explicitly requests the OpenAI reasoning path. Use this when you want to guarantee the demo is configured for the LLM. |
| `deterministic` | Never calls a hosted model. Useful for CI, benchmarks, development, and zero-cost demos. |

The default model is `gpt-5-mini`. Override it with `SENTINELOPS_MODEL` or the CLI `--model` flag.

`SENTINELOPS_LLM_FALLBACK=true` allows a transient model/API failure to fall back to deterministic investigation/planning. The deterministic Reviewer, PolicyEngine, Remediator, and Verifier are used in every mode.

## Requirements

- Python **3.11+**
- `pip`
- An **OpenAI API key** for real LLM-backed investigation and planning
- Optional: Docker for the containerized demo
- No AWS/Kubernetes/cloud credentials are required for the built-in simulator

The tests and `sentinelops eval` intentionally do **not** require or call the OpenAI API.

## Fresh-clone setup

Clone the repository:

```bash
git clone https://github.com/rzaman8677/agent.git
cd agent
```

If PR #1 has not been merged yet, check out the feature branch:

```bash
git checkout agent/sentinelops-control-plane
```

Enter the project:

```bash
cd sentinelops
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[all]'
cp .env.example .env
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
Copy-Item .env.example .env
```

If PowerShell blocks virtual-environment activation for the current shell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Configure OpenAI credentials

Open `.env` and set your API key:

```env
SENTINELOPS_AGENT_BACKEND=auto
SENTINELOPS_MODEL=gpt-5-mini
SENTINELOPS_LLM_FALLBACK=true
SENTINELOPS_LLM_TIMEOUT_SECONDS=30
OPENAI_API_KEY=your_api_key_here
```

Do **not** commit `.env`. It is already ignored by `.gitignore`; only `.env.example` belongs in Git.

To force the LLM path instead of `auto`:

```env
SENTINELOPS_AGENT_BACKEND=openai
```

To explicitly run without an LLM:

```env
SENTINELOPS_AGENT_BACKEND=deterministic
```

## 1. Verify the installation

Run the non-billable self-check:

```bash
sentinelops doctor
```

It validates:

- Python version
- packaged runbooks
- tool registry
- simulator baseline health
- SHA-256 event-chain integrity
- LLM configuration / active backend

With an API key and the full install, the LLM section should report an active backend of `openai`.

## 2. Verify the OpenAI credential with a live request

```bash
sentinelops llm-check
```

This performs one small structured-output request through the OpenAI Responses API. A successful result includes:

```json
{
  "ok": true,
  "model": "gpt-5-mini",
  "response_id": "resp_..."
}
```

If this succeeds, the same OpenAI adapter used by the Investigator and Planner is working.

## 3. Run the test suite

```bash
python -m pytest
```

The tests are offline and do not use your API key. They cover:

- assisted vs. autonomous policy behavior
- hard high-risk approval gates
- human approval pause/resume
- autonomous capacity recovery
- hash-chain integrity
- runbook retrieval
- runtime/readiness diagnostics
- FastAPI incident/approval/recovery flow
- OpenAI Responses API request shape using a fake client
- mocked LLM Investigator + Planner end-to-end recovery
- benchmark regression thresholds

## 4. Run the deterministic agent benchmark

```bash
sentinelops eval
```

The benchmark is deliberately pinned to the deterministic backend so it is reproducible and never spends API credits, even when `.env` contains a valid key.

It reports:

- `resolution_rate`
- `tool_selection_accuracy`
- `unsafe_action_rate`
- `trace_integrity_rate`

Validated baseline:

```text
resolution_rate          = 1.0
tool_selection_accuracy  = 1.0
unsafe_action_rate       = 0.0
trace_integrity_rate     = 1.0
```

GitHub Actions runs the tests and benchmark on Python 3.11, 3.12, and 3.13 without requiring GitHub secrets.

## 5. Run a real LLM-backed incident demo

With `OPENAI_API_KEY` in `.env`, the default `auto` backend uses OpenAI.

### Human-in-the-loop bad deployment

```bash
sentinelops demo --fault bad_deployment --service checkout --mode assisted
```

The OpenAI Investigator reasons over the injected error/latency telemetry and deployment logs. The OpenAI Planner proposes a bounded rollback. The deterministic policy layer should pause at:

```text
awaiting_approval
```

Run with approval:

```bash
sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve
```

Expected remediation: `rollback_deployment`; expected final status: `resolved`.

### Autonomous capacity recovery

```bash
sentinelops demo --fault capacity --service payments --mode autonomous
```

Expected LLM proposal: `scale_service`; expected final status: `resolved`.

### Autonomous crash-loop recovery

```bash
sentinelops demo --fault crashloop --service catalog --mode autonomous
```

Expected LLM proposal: `restart_service`; expected final status: `resolved`.

### Guarantee the CLI requests OpenAI

```bash
sentinelops demo --fault capacity --service payments --mode autonomous --backend openai
```

You can also override the model:

```bash
sentinelops demo --fault capacity --service payments --mode autonomous --backend openai --model gpt-5-mini
```

The CLI output contains an `llm` object. Confirm:

```json
{
  "active_backend": "openai"
}
```

The trace should also contain:

- `investigator.completed` with `reasoning.backend = openai`
- `planner.llm_completed` with the model and OpenAI response ID
- `policy.decision`
- `remediator.executed`
- `verifier.completed`

No API key is written to the trace.

### Force an offline deterministic demo

```bash
sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve --backend deterministic
```

## 6. Run the API and dashboard

Start the control plane:

```bash
sentinelops serve
```

If `.env` contains `OPENAI_API_KEY` and the backend is `auto` or `openai`, API-triggered incident response uses the LLM Investigator and Planner.

To explicitly force OpenAI:

```bash
sentinelops serve --backend openai
```

Then open:

```text
http://127.0.0.1:8000
```

The dashboard can inject faults, launch incident response, approve gated actions, inspect execution traces, view simulator state, and run the deterministic benchmark.

Useful endpoints:

- `GET /health` — liveness plus active LLM backend/model
- `GET /ready` — readiness/self-diagnostics including LLM configuration
- `GET /api/metrics` — incident counts, trace integrity, simulator summary, and LLM backend status
- `GET /api/incidents` — list incidents
- `POST /api/incidents` — create an incident from an alert/SLO signal
- `POST /api/incidents/{id}/respond` — run LLM investigation/planning, deterministic review, and policy checking
- `POST /api/incidents/{id}/approve` — resume approval-gated remediation
- `GET /api/incidents/{id}/trace` — inspect and verify the event stream
- `POST /api/simulator/faults` — inject a deterministic fault
- `GET /api/simulator/state` — inspect simulated cloud state
- `GET /api/evals` — run the offline safety/recovery benchmark

### API smoke tests

With the server running in another terminal:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/api/metrics
curl http://127.0.0.1:8000/api/evals
```

Confirm `/health` reports:

```json
{
  "llm": {
    "active_backend": "openai"
  }
}
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

## 7. Run with Docker

Build the image:

```bash
docker build -t sentinelops .
```

Run with your local `.env` file so the container can access the OpenAI credential:

```bash
docker run --rm --env-file .env -p 8000:8000 sentinelops
```

Then open `http://127.0.0.1:8000` and verify:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Do not bake `.env` or an API key into the image.

## 8. Optional Make targets

```bash
make install
make doctor
make llm-check
make test
make eval
make demo
make demo-deterministic
make serve
```

## How the LLM is kept safe

The OpenAI model is deliberately **not** given direct authority over the tool registry.

1. Investigator receives read-only observations: incident signals, metrics, logs, health, and retrieved runbooks.
2. Structured output constrains its finding types.
3. Findings are grounded against deterministic telemetry signatures before they can influence a remediation.
4. Planner receives the grounded findings and a small allowlist of write tools.
5. Structured output constrains the planner to the allowlisted action schema.
6. Reviewer rejects wrong-service, duplicate, out-of-range, or root-cause-inconsistent proposals.
7. PolicyEngine independently applies autonomy mode, risk, blast-radius, and confidence gates.
8. Remediator is the only layer that actually invokes a write tool and maintains the idempotency ledger.
9. Verifier independently checks service health/SLO recovery after execution.
10. Every stage is appended to the tamper-evident event stream.

If the LLM returns malformed output or the API is temporarily unavailable, `SENTINELOPS_LLM_FALLBACK=true` permits deterministic fallback. Authorization never falls back to the model.

## Deterministic evaluation scenarios

| Incident class | Injected symptom | Expected action |
| --- | --- | --- |
| Regression after deployment | error/latency spike after release | `rollback_deployment` |
| CPU / queue saturation | high CPU, queue depth, p95 latency | `scale_service` |
| Crash-looping replicas | unhealthy replicas / exit 137 | `restart_service` |

## What to inspect during a demo

```text
sentinelops/llm.py           OpenAI Responses API adapter + structured outputs
sentinelops/agents.py        LLM investigator/planner + deterministic reviewer/remediator/verifier
sentinelops/core.py          policy engine, simulator, tool registry, event chain
sentinelops/orchestrator.py  deterministic incident lifecycle + LLM wiring
sentinelops/evals.py         offline recovery + safety benchmark
sentinelops/diagnostics.py   readiness and LLM configuration checks
sentinelops/api.py           FastAPI control plane
runbooks.json                operational grounding
web/index.html               visual control plane
```

A strong interview demo sequence is:

1. show `.env.example` without exposing the real key
2. `sentinelops doctor`
3. `sentinelops llm-check`
4. run an assisted bad-deployment incident without `--approve`
5. point out the OpenAI Investigator/Planner events in the trace
6. show that the deterministic policy pauses at `awaiting_approval`
7. rerun with approval and show rollback + independent SLO verification
8. run `sentinelops eval` and explain why the regression benchmark intentionally stays deterministic
9. start the dashboard and inject a second failure visually

## Troubleshooting

### `sentinelops: command not found`

Activate the virtual environment and reinstall:

```bash
python -m pip install -e '.[all]'
```

You can also invoke the CLI directly:

```bash
python -m sentinelops.cli doctor
```

### `sentinelops llm-check` says the OpenAI backend is not enabled

Check that `.env` exists in the `sentinelops` directory and contains:

```env
OPENAI_API_KEY=your_api_key_here
SENTINELOPS_AGENT_BACKEND=auto
```

Then rerun:

```bash
sentinelops doctor
sentinelops llm-check
```

### Invalid API key / authentication error

Create or replace the OpenAI API key in `.env`. Do not add quotes or commit the file. `sentinelops llm-check` makes the smallest direct connectivity test before you run a full incident.

### Model access error

Set a model available to your OpenAI API project:

```env
SENTINELOPS_MODEL=gpt-5-mini
```

or override it for a single demo:

```bash
sentinelops demo --fault capacity --service payments --mode autonomous --backend openai --model gpt-5-mini
```

### `No module named openai`, `fastapi`, or `uvicorn`

Install the full dependency set:

```bash
python -m pip install -e '.[all]'
```

### Port 8000 is already in use

```bash
sentinelops serve --port 8080
```

Then open `http://127.0.0.1:8080`.

### Resetting demo state

Each CLI demo creates a fresh in-memory simulator/orchestrator. Restarting `sentinelops serve` resets API/dashboard simulator and incident state.

## Production extension points

The same interfaces can replace the simulator with Kubernetes/EKS/ECS/AWS control-plane adapters and the in-memory event stream with Kafka, DynamoDB, or Postgres. A production deployment would add durable workflows, external idempotency storage, RBAC-signed approvals, OpenTelemetry, per-tool circuit breakers, secret-manager/workload identity integration, model-call budgets, prompt/version tracking, and least-privilege IAM.

The key architecture invariant should remain unchanged: **the model proposes; deterministic policy authorizes; tools execute; independent verification closes the loop.**

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

## Resume bullet

> Built SentinelOps, a policy-gated multi-agent SRE control plane using OpenAI structured reasoning to diagnose production incidents and generate grounded remediation plans; constrained model proposals behind deterministic semantic review, blast-radius/risk/confidence policy gates, human approval, idempotent tool execution, independent SLO verification, SHA-256 hash-chained traces, and offline CI safety/recovery evals.
