# SentinelOps Architecture

SentinelOps is structured as a reliability control plane rather than a single conversational agent. OpenAI reasoning is used where ambiguity and synthesis are valuable; execution state, authorization, side effects, and recovery verification remain deterministic.

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

The `SentinelOrchestrator` owns lifecycle transitions. Individual agents and the LLM cannot directly declare an incident resolved.

## Reasoning backend

`OpenAIReasoner` wraps the OpenAI Responses API. It loads `OPENAI_API_KEY` from the environment, uses `SENTINELOPS_MODEL` (default `gpt-5-mini`), and requests strict JSON-schema outputs.

`SENTINELOPS_AGENT_BACKEND` selects runtime behavior:

- `auto`: OpenAI when a key/runtime is available, deterministic otherwise.
- `openai`: require the OpenAI path; missing local configuration is an error.
- `deterministic`: never make hosted-model calls.

The offline evaluation harness explicitly injects a deterministic reasoner so CI remains reproducible and secret-free regardless of a developer's local `.env`.

## Agent roles

**Investigator** collects metrics, service health, logs, alert signals, and retrieved runbooks. When OpenAI is enabled it asks the model for structured findings, then grounds those finding types against deterministic telemetry signatures before returning them to the orchestrator. Unsupported model diagnoses are discarded when stronger deterministic evidence exists.

**Planner** sends the grounded findings and current metrics to OpenAI and requests a structured remediation proposal. The schema restricts the model to the supported write tools and bounded arguments. A semantic consistency check filters proposals that conflict with the observed root cause before the Reviewer receives them.

**Reviewer** is deterministic. It rejects duplicate, cross-service, root-cause-inconsistent, or invalidly bounded actions.

**Remediator** is the only layer that executes state-changing tools. It maintains an idempotency ledger so workflow retries cannot duplicate the same side effect.

**Verifier** independently re-reads health and SLO metrics after remediation. Tool success alone is not enough to mark recovery.

## Deterministic policy boundary

`PolicyEngine` is intentionally not an LLM. It gates actions by autonomy mode, risk, blast radius, confidence, and per-incident action budget. High and critical risk actions always require approval.

The model never receives an authorization primitive. It proposes; the Reviewer validates; the PolicyEngine authorizes; the Remediator executes; the Verifier closes the loop.

## Event sourcing and auditability

Every material transition, reasoning stage, policy decision, approval, and tool action is appended to `EventStore`. Each event includes the hash of the previous event plus a canonical representation of the current event, producing a SHA-256 tamper-evident chain.

LLM events record non-secret metadata such as backend, model, response ID, latency, and accepted proposals. API keys are never added to the event payload.

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

The LLM does not directly call these functions. A production adapter can implement the same contract for Kubernetes, ECS, CloudWatch, Datadog, or an internal platform without moving authorization into the model.

## Runbook retrieval

The baseline retriever uses lexical relevance plus a service-match boost. Retrieved runbooks are included in the Investigator context to ground model reasoning while keeping retrieval itself deterministic and evaluation-friendly.

## Failure behavior

When the OpenAI backend is active and a model request fails, `SENTINELOPS_LLM_FALLBACK=true` allows the Investigator or Planner to use the deterministic reasoning path. If fallback is disabled, the model error propagates rather than silently inventing a plan. Forced `openai` mode also fails startup when the local OpenAI runtime/key is unavailable.

## Production extension path

A production version would add durable workflow execution, distributed leases, external idempotency storage, tool-specific retry/circuit-breaker policy, RBAC-signed approvals, OpenTelemetry traces, workload identity, prompt/version provenance, model-call budgets, and durable incident/event persistence. Those concerns can be added behind existing interfaces without moving policy authority into the model.
