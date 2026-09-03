output "sentinelops_url" {
  value = google_cloud_run_v2_service.sentinelops.uri
}

output "alert_topic" {
  value = google_pubsub_topic.alerts.id
}

output "dead_letter_topic" {
  value = google_pubsub_topic.dead_letter.id
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}
