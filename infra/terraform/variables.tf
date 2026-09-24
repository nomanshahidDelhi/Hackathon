variable "project_id" {
  description = "GCP project ID for the team."
  type        = string
}

variable "region" {
  description = "Single region for BigQuery datasets, the vertex_conn connection and Cloud Run. BigQuery datasets are bound to it, so pick once (per lab guidelines)."
  type        = string
  default     = "us-central1"
}

variable "alerts_topic" {
  description = "Pub/Sub topic that receives streamed alerts (M3 ingest)."
  type        = string
  default     = "alerts-in"
}
