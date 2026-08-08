# SentinelOps Architecture

SentinelOps is structured as a small reliability control plane rather than a single conversational agent. The model/reasoning layer is replaceable; execution state, safety, side effects, and verification are deterministic.

## Incident lifecycle

```text
DETECTED
  -> INVESTIGATING
  -> PLANNING
  -> AWAITING_APPROVAL (optional)
  -> EXECUTING
  -> VERIFYING
  -> RESOLVED | ESCALATED
```

The `SentinelOrchestrator` owns lifecycle transitions. Individual agents cannot directly declare an incident resolved.

## Agent roles

**Investigator** collects metrics, service health, logs, and retrieved runbooks, then emits structured findings with evidence/confidence.

**Planner** maps findings to bounded `ActionProposal` objects containing tool, arguments, rationale, expected effect, risk, blast radius, confidence, and an idempotency key.

**Reviewer** acts as a second boundary before execution. It rejects duplicate, cross-service, or invalidly bounded actions.

**Remediator** is the only agent that executes state-changing tools. It maintains an idempotency ledger so workflow retries cannot duplicate the same side effect.

**Verifier** independently re-reads health and SLO metrics after remediation. Tool success alone is not enough to mark recovery.

## Deterministic policy boundary

`PolicyEngine` is intentionally not an LLM. It gates actions by autonomy mode, risk, blast radius, confidence, and per-incident action budget. High and critical risk actions always require approval.

This keeps authorization outside the reasoning model and makes safety behavior reproducible in tests.

## Event sourcing and auditability

Every material transition, policy decision, and tool action is appended to `EventStore`. Each event includes the hash of the previous event plus a canonical representation of the current event, producing a SHA-256 tamper-evident chain.

The demo store is in memory, but the interface is intentionally narrow enough to replace with Kafka, DynamoDB, or an append-only Postgres table.

## Tool boundary

The simulator exposes read-only diagnostic tools:

- `get_service_health`
- `get_metrics`
- `query_logs`

and state-changing remediation tools:

- `restart_service`
- `scale_service`
- `rollback_deployment`

A production adapter can implement the same contract for Kubernetes, ECS, CloudWatch, Datadog, or an internal platform.

## Runbook retrieval

The deterministic baseline uses lexical relevance plus a service-match boost. That keeps evaluation repeatable while preserving a clean retrieval interface for later hybrid BM25/vector search.

## Production extension path

A production version would add durable workflow execution, distributed leases, external idempotency storage, tool-specific retry/circuit-breaker policy, RBAC-signed approvals, OpenTelemetry traces, workload identity, and durable incident/event persistence. Those concerns can be added behind existing interfaces without moving policy authority into the model.
