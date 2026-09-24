data "google_project" "this" {
  project_id = var.project_id
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------
locals {
  services = [
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "bigquerystorage.googleapis.com",
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# BigQuery -> Vertex AI connection used by sre_knowledge_base.embedding_model.
# Must live in the same location as the datasets. The kit's step 04 fails
# until the aiplatform.user grant below has propagated (a few minutes); the
# loader retries for that.
# ---------------------------------------------------------------------------
resource "google_bigquery_connection" "vertex_conn" {
  connection_id = "vertex_conn"
  location      = var.region
  friendly_name = "vertex_conn"
  description   = "Remote-model connection for text-embedding-005 (Track 2 runbook embeddings)."
  cloud_resource {}

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "vertex_conn_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_bigquery_connection.vertex_conn.cloud_resource[0].service_account_id}"
}

# ---------------------------------------------------------------------------
# Service accounts, one per runtime. Datasets are created by the loader (SQL
# owns the warehouse), so roles are granted at project level.
# ---------------------------------------------------------------------------
locals {
  service_accounts = {
    "sre-agent" = {
      display = "SRE agent backend (ADK on Cloud Run)"
      roles = [
        "roles/bigquery.dataEditor",
        "roles/bigquery.jobUser",
        "roles/bigquery.connectionUser",
        "roles/aiplatform.user",
        "roles/run.invoker",
      ]
    }
    "sre-executor" = {
      display = "Remediation sandbox executor"
      roles = [
        "roles/bigquery.dataEditor",
        "roles/bigquery.jobUser",
      ]
    }
    "sre-ingest" = {
      display = "Alert stream ingest consumer"
      roles = [
        "roles/bigquery.dataEditor",
        "roles/bigquery.jobUser",
        "roles/pubsub.subscriber",
      ]
    }
  }

  sa_role_pairs = flatten([
    for sa, cfg in local.service_accounts : [
      for role in cfg.roles : { sa = sa, role = role }
    ]
  ])
}

resource "google_service_account" "runtime" {
  for_each     = local.service_accounts
  account_id   = each.key
  display_name = each.value.display

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = { for p in local.sa_role_pairs : "${p.sa}|${p.role}" => p }
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.runtime[each.value.sa].email}"
}

# ---------------------------------------------------------------------------
# Pub/Sub for the M3 alert stream, with a dead-letter topic so nothing is
# silently dropped.
# ---------------------------------------------------------------------------
resource "google_pubsub_topic" "alerts" {
  name       = var.alerts_topic
  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "alerts_dlq" {
  name       = "${var.alerts_topic}-dlq"
  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_subscription" "alerts_ingest" {
  name                       = "${var.alerts_topic}-ingest"
  topic                      = google_pubsub_topic.alerts.id
  ack_deadline_seconds       = 60
  message_retention_duration = "86400s"

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.alerts_dlq.id
    max_delivery_attempts = 10
  }

  retry_policy {
    minimum_backoff = "5s"
    maximum_backoff = "120s"
  }
}

resource "google_pubsub_subscription" "alerts_dlq_hold" {
  name                       = "${var.alerts_topic}-dlq-hold"
  topic                      = google_pubsub_topic.alerts_dlq.id
  message_retention_duration = "604800s"
}

# The Pub/Sub service agent must be able to forward to the DLQ and ack the source.
locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.alerts_dlq.id
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "source_subscriber" {
  subscription = google_pubsub_subscription.alerts_ingest.id
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

# ---------------------------------------------------------------------------
# Container registry for the Cloud Run services (agent, executor, ingest, console).
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "sre-agent"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled]
}
