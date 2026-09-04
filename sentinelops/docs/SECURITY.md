# SentinelOps Safety Model

SentinelOps treats all model output as **untrusted proposals** until it passes deterministic checks. The OpenAI model participates in diagnosis and planning; it never owns authorization or direct infrastructure credentials.

## LLM trust boundary

The LLM-backed Investigator receives read-only incident context: alert signals, provider telemetry, logs, service health, and retrieved runbooks. Structured output constrains the finding schema, and model findings are grounded against observed deterministic telemetry signatures before they can drive remediation.

The LLM-backed Planner receives the grounded findings plus a small allowlist of supported write tools. It can propose only `rollback_deployment`, `scale_service`, or `restart_service`. A deterministic Reviewer rejects wrong-service, duplicate, out-of-range, or root-cause-inconsistent proposals before policy evaluation.

The model is never given an API that directly executes these tools.

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
- root-cause/action consistency is checked independently of the LLM.

## LLM failure behavior

`SENTINELOPS_LLM_FALLBACK=true` permits deterministic investigation/planning when a configured model call fails. The policy engine itself never falls back to model judgment. `SENTINELOPS_AGENT_BACKEND=openai` requires a working local OpenAI configuration at startup; `auto` permits an offline deterministic runtime when no API key is configured.

The deterministic CI/eval path never makes hosted model calls, which prevents tests from requiring secrets, spending API credits, or becoming nondeterministic.

## Credential handling

- `OPENAI_API_KEY` belongs only in a local `.env`, shell environment, secret manager, or deployment secret.
- `.env` is ignored by Git.
- `.dockerignore` excludes `.env` and `.env.*` from the Docker build context while allowing `.env.example`.
- Docker runtime credentials should be passed with `--env-file .env` or an orchestrator secret, never baked into the image.
- API keys are never written into incident events or traces.

## Idempotency

Every remediation proposal has an idempotency key. The remediator records the first execution result and returns the same result for a duplicate request, modeling protection against retries after worker/network failures.

## Human approval binding

When policy requires approval, SentinelOps stores a SHA-256 hash of the canonical pending action list. The approval request must include that exact hash and a non-empty approver identity. A changed, stale, or corrupted plan is rejected before the remediator receives it. In production, Cloud Run IAM or an identity-aware gateway must authenticate the caller and supply or validate the recorded identity; a client-provided string alone is audit context, not proof of identity.

## Auditability

Incident events are SHA-256 hash chained. LLM-stage events record non-secret metadata such as model name, response ID, latency, accepted proposal, and backend. The verifier can recompute the chain to detect mutation of prior trace entries.

## Closed-loop recovery

The system never equates a successful tool response with incident resolution. A separate verifier performs bounded polling, re-reads service health, and checks each original incident metric against its stated SLO threshold; exhausted verification attempts escalate the incident.

## Real infrastructure adapters

AWS, GCP, and Kubernetes SDK clients use ambient workload credentials. Firestore provides durable incident, event, and idempotency state. Missing telemetry fails closed, provider exceptions become failed tool results, and write calls remain behind deterministic review and policy.

Do not give the public API container broad production write access in a sensitive deployment. Put the remediator behind a separately authenticated service with a resource-scoped write identity. Also bind the recorded approver to a verified OIDC/RBAC principal, add per-tool rate limits and circuit breakers, network egress controls, redaction and prompt-injection defenses for untrusted logs/runbooks, model-call budgets, prompt/version provenance, and a security review for every enabled mutation adapter.
