# Resume claim evidence

This file maps each Autonomous Incident Response Agent resume phrase to executable evidence. It intentionally separates offline proof from live-cloud proof.

| Resume phrase | Executable evidence | What the evidence proves |
| --- | --- | --- |
| Policy-gated multi-agent SRE control plane | `tests/test_system.py::test_llm_investigator_and_planner_path_resolves_incident` and the 100-case eval | Investigator, Planner, Reviewer, Remediator, and Verifier participate in a traced lifecycle; model output does not authorize itself. |
| OpenAI diagnosis from telemetry, runbooks, and service metrics | Fake Responses client plus scripted OpenAI-path test | Strict JSON-schema calls receive incident signals, metrics, logs, health, and retrieved runbooks without making a billable request in CI. |
| Proposes rollback, scaling, or restart | 75 recovery eval cases | Each fault family selects exactly its expected tool and reaches verified recovery. |
| Deterministic safety gates | 25 adversarial eval cases | Wrong-service, out-of-range, root-cause-incompatible, high-risk, and excessive-blast-radius proposals never execute silently. |
| GCP Cloud Run, Pub/Sub, and Firestore | mocked Cloud Run/Pub/Sub/Firestore integration tests plus `deploy/gcp` | API contracts, redelivery deduplication, durable pause/resume, trace persistence, idempotency, and reproducible infrastructure wiring. |
| Human approvals | API and Firestore restart/resume tests | No mutation occurs before approval; an approver and SHA-256 hash bind the approval to the exact pending plan. |
| Idempotent remediation | duplicate execution and Firestore-ledger tests | Reusing an action key returns the prior result without a second provider mutation. |
| SLO verification | delayed-recovery polling test and recovery evals | A successful mutation is insufficient: the original incident metric must return within its threshold before resolution. |
| Hash-chained traces | in-memory tamper test and mocked Firestore event-stream test | Reordering or modifying prior event content invalidates SHA-256 chain verification. |
| 100% tool accuracy and 0 unsafe actions in 100 CI evals | `sentinelops eval` and GitHub Actions assertions | Exactly 100 deterministic cases must report `case_pass_rate=1.0`, `tool_selection_accuracy=1.0`, `unsafe_action_count=0`, and `trace_integrity_rate=1.0`. |

## Verification commands

```bash
python -m pytest
sentinelops eval
sentinelops doctor
```

Offline tests cannot prove that a live GCP deployment currently exists or that real credentials have the intended IAM scope. To substantiate the word "deployed," apply `deploy/gcp` to a staging project, run one canary incident, retain the Cloud Run revision and CI run links, and capture the verified incident trace. A real OpenAI request is separately checked with `sentinelops llm-check`; CI uses mocks so it remains deterministic and secret-free.
