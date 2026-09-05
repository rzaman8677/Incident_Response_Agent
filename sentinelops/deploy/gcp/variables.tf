variable "project_id" {
  description = "GCP project that hosts SentinelOps."
  type        = string
}

variable "region" {
  description = "Cloud Run region."
  type        = string
  default     = "us-central1"
}

variable "container_image" {
  description = "Immutable SentinelOps production image URI."
  type        = string
}

variable "service_name" {
  description = "Cloud Run control-plane service name."
  type        = string
  default     = "sentinelops"
}

variable "alert_topic_name" {
  description = "Pub/Sub topic receiving monitoring alerts."
  type        = string
  default     = "sentinelops-alerts"
}

variable "openai_secret_id" {
  description = "Secret Manager secret containing the OpenAI API key."
  type        = string
  default     = "sentinelops-openai-api-key"
}

variable "firestore_database" {
  description = "Firestore database used for durable state."
  type        = string
  default     = "(default)"
}

variable "firestore_location" {
  description = "Firestore location used only when database creation is enabled."
  type        = string
  default     = "nam5"
}

variable "create_firestore_database" {
  description = "Create the Firestore database; leave false when it already exists."
  type        = bool
  default     = false
}

variable "managed_service_names" {
  description = "Cloud Run services SentinelOps may remediate."
  type        = set(string)
  default     = []
}

variable "deletion_protection" {
  description = "Protect the SentinelOps Cloud Run service from accidental deletion."
  type        = bool
  default     = true
}

variable "managed_service_account_emails" {
  description = "Runtime service accounts on managed Cloud Run services; updates require iam.serviceAccounts.actAs."
  type        = set(string)
  default     = []
}
