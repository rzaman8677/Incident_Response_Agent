# SentinelOps

**Policy-gated multi-agent incident response for cloud services.** SentinelOps investigates production failures, retrieves operational runbooks, proposes bounded remediation, gates state-changing actions behind deterministic safety policy, executes idempotently, and independently verifies SLO recovery.

This project is intentionally more than an LLM/tool demo. It models the systems problems that appear once agents can mutate real infrastructure: lifecycle state, retries, idempotency, blast radius, auditability, human approval, post-action verification, and regression evaluation.

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
- **CLI + FastAPI + dashboard:** terminal, service, and visual demo surfaces.

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

## Quick start

```bash
cd sentinelops
python -m venv .venv
source .venv/bin/activate
pip install -e '.[all]'

# Human-in-the-loop incident response
sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve

# Fully autonomous, policy-bounded response
sentinelops demo --fault capacity --service payments --mode autonomous

# Safety and recovery benchmark
sentinelops eval

# API + dashboard
sentinelops serve
```

Then open `http://127.0.0.1:8000`.

## Deterministic evaluation baseline

| Incident class | Expected action |
| --- | --- |
| Regression after deployment | `rollback_deployment` |
| CPU / queue saturation | `scale_service` |
| Crash-looping replicas | `restart_service` |

The validated baseline is:

- **Resolution rate:** 100%
- **Tool-selection accuracy:** 100%
- **Unsafe-action rate:** 0%
- **Trace-integrity rate:** 100%

CI runs this benchmark as a regression gate in addition to the unit/system tests.

## API

- `POST /api/simulator/faults` — inject a deterministic incident
- `POST /api/incidents` — create an incident from an alert/SLO signal
- `POST /api/incidents/{id}/respond` — investigate, plan, review, and policy-check
- `POST /api/incidents/{id}/approve` — resume approval-gated remediation
- `GET /api/incidents/{id}/trace` — inspect and verify the event stream
- `GET /api/evals` — run the safety/recovery benchmark

## Production extension points

The interfaces are designed so the simulator can be replaced with Kubernetes/AWS control-plane adapters, the event stream with Kafka/DynamoDB/Postgres, and the deterministic planner with a hosted LLM without moving authorization into the model. A production build would add durable workflows, external idempotency storage, RBAC-signed approvals, OpenTelemetry, per-tool circuit breakers, and least-privilege workload identity.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

## Resume bullet

> Built SentinelOps, a policy-gated multi-agent SRE control plane that diagnoses production incidents, retrieves operational runbooks, generates bounded remediation plans, executes idempotent actions behind human/autonomous safety gates, verifies SLO recovery, and records SHA-256 hash-chained execution traces; added a CI benchmark suite measuring recovery accuracy and unsafe-action rate.
