output "vertex_conn_service_account" {
  description = "Service agent of vertex_conn; holds roles/aiplatform.user."
  value       = google_bigquery_connection.vertex_conn.cloud_resource[0].service_account_id
}

output "runtime_service_accounts" {
  value = { for k, sa in google_service_account.runtime : k => sa.email }
}

output "alerts_topic" {
  value = google_pubsub_topic.alerts.id
}

output "image_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
