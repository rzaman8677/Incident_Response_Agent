# SentinelOps Safety Model

SentinelOps treats agent output as untrusted until it passes deterministic checks.

## Autonomy modes

| Mode | Diagnostics | State-changing actions |
| --- | --- | --- |
| `observe` | allowed | blocked / approval required |
| `assisted` | allowed | human approval required |
| `autonomous` | allowed | only inside configured policy bounds |

## Hard gates

Even in autonomous mode:

- `HIGH` and `CRITICAL` risk actions require approval.
- blast radius cannot exceed the configured limit.
- confidence must meet the autonomous threshold.
- a per-incident action budget prevents unbounded remediation loops.
- reviewer validation prevents cross-service mutations and invalid scale values.

## Idempotency

Every remediation proposal has an idempotency key. The remediator records the first execution result and returns the same result for a duplicate request, modeling protection against retries after worker/network failures.

## Auditability

Incident events are SHA-256 hash chained. The verifier can recompute the chain to detect mutation of prior trace entries.

## Closed-loop recovery

The system never equates a successful tool response with incident resolution. A separate verifier re-reads service health and SLO metrics; failed verification escalates the incident.

## Production hardening before real infrastructure

Add workload identity and least-privilege IAM, signed/RBAC approval identity, durable event and idempotency storage, tool argument schemas, rate limits/circuit breakers, network egress controls, prompt-injection defenses for external logs/runbooks, secret-manager integration, and security review for each state-changing adapter.
