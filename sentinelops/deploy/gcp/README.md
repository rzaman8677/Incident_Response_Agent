# GCP deployment

This Terraform module wires the production path claimed by SentinelOps:

- a private-by-default Cloud Run v2 control plane using the production Docker image;
- an authenticated Pub/Sub push subscription for alert ingestion;
- Firestore configuration for incidents, pending approvals, traces, and idempotency;
- Secret Manager injection for `OPENAI_API_KEY`;
- a dedicated runtime identity plus a separate Pub/Sub invocation identity;
- explicit Cloud Run service-level write grants only for names in `managed_service_names`;
- retry and dead-letter handling for failed Pub/Sub deliveries.

Build and push the image with all provider dependencies before applying:

```bash
docker build --build-arg SENTINELOPS_EXTRAS=production -t REGION-docker.pkg.dev/PROJECT/REPOSITORY/sentinelops:TAG ../..
docker push REGION-docker.pkg.dev/PROJECT/REPOSITORY/sentinelops:TAG
```

Create the OpenAI secret separately and do not place its value in Terraform variables or state:

```bash
printf '%s' "$OPENAI_API_KEY" | gcloud secrets versions add sentinelops-openai-api-key --data-file=-
```

Apply with an immutable image tag or digest and an explicit target allowlist:

```bash
terraform init
terraform plan \
  -var='project_id=YOUR_PROJECT' \
  -var='container_image=us-central1-docker.pkg.dev/YOUR_PROJECT/agents/sentinelops:TAG' \
  -var='managed_service_names=["checkout","payments","catalog"]'
terraform apply
```

Set `create_firestore_database=true` only when the selected database does not already exist. After apply, publish the documented incident JSON to the `alert_topic` output and confirm the incident is persisted, pauses for a matching approval, executes once, and produces a valid trace.

## Managed service runtime identities

Set `managed_service_account_emails` to the runtime service-account emails of the services in `managed_service_names`. Cloud Run service updates require `iam.serviceAccounts.actAs` for these identities. The template grants `roles/iam.serviceAccountUser` only on the named accounts. Also ensure the three custom Cloud Monitoring metrics described in `docs/PRODUCTION_INTEGRATIONS.md` actually exist before expecting diagnosis and SLO checks to work.
