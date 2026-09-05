terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  runtime_roles = toset([
    "roles/datastore.user",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/run.viewer",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = "sentinelops-runtime"
  display_name = "SentinelOps runtime"
}

resource "google_service_account" "pubsub_push" {
  project      = var.project_id
  account_id   = "sentinelops-pubsub"
  display_name = "SentinelOps Pub/Sub push identity"
}

resource "google_project_iam_member" "runtime" {
  for_each = local.runtime_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "openai" {
  project   = var.project_id
  secret_id = var.openai_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_firestore_database" "state" {
  count                       = var.create_firestore_database ? 1 : 0
  project                     = var.project_id
  name                        = var.firestore_database
  location_id                 = var.firestore_location
  type                        = "FIRESTORE_NATIVE"
  delete_protection_state     = "DELETE_PROTECTION_ENABLED"
  deletion_policy             = "ABANDON"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "sentinelops" {
  project             = var.project_id
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.runtime.email
    timeout         = "300s"

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "SENTINELOPS_AGENT_BACKEND"
        value = "auto"
      }
      env {
        name  = "SENTINELOPS_AUTONOMY"
        value = "assisted"
      }
      env {
        name  = "SENTINELOPS_INFRA_BACKEND"
        value = "gcp_cloud_run"
      }
      env {
        name  = "SENTINELOPS_STATE_BACKEND"
        value = "firestore"
      }
      env {
        name  = "SENTINELOPS_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "SENTINELOPS_GCP_REGION"
        value = var.region
      }
      env {
        name  = "SENTINELOPS_FIRESTORE_DATABASE"
        value = var.firestore_database
      }
      env {
        name  = "SENTINELOPS_VERIFICATION_ATTEMPTS"
        value = "18"
      }
      env {
        name  = "SENTINELOPS_VERIFICATION_INTERVAL_SECONDS"
        value = "10"
      }
      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = var.openai_secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_project_iam_member.runtime,
    google_secret_manager_secret_iam_member.openai,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.sentinelops.location
  name     = google_cloud_run_v2_service.sentinelops.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_push.email}"
}

resource "google_cloud_run_v2_service_iam_member" "managed_service_developer" {
  for_each = var.managed_service_names
  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic" "alerts" {
  project = var.project_id
  name    = var.alert_topic_name

  depends_on = [google_project_service.required]
}

resource "google_pubsub_subscription" "alerts_push" {
  project              = var.project_id
  name                 = "${var.alert_topic_name}-sentinelops-push"
  topic                = google_pubsub_topic.alerts.id
  ack_deadline_seconds = 60

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 10
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.sentinelops.uri}/api/events/pubsub"
    oidc_token {
      service_account_email = google_service_account.pubsub_push.email
      audience              = google_cloud_run_v2_service.sentinelops.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.pubsub_invoker,
    google_service_account_iam_member.pubsub_token_creator,
  ]
}

resource "google_pubsub_topic" "dead_letter" {
  project = var.project_id
  name    = "${var.alert_topic_name}-dead-letter"
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.alerts_push.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_service_account_iam_member" "managed_service_act_as" {
  for_each           = var.managed_service_account_emails
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime.email}"
}
