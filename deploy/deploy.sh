#!/usr/bin/env bash
# Build and deploy to Cloud Run from Cloud Shell.
#
#   export GCP_PROJECT_ID=... GCP_REGION=us-central1
#   ./deploy/deploy.sh            # console + agent backend (service: sre-console)
#   ./deploy/deploy.sh ingest     # also the Pub/Sub consumer (service: sre-ingest)
#
# The console is deployed WITHOUT public access: approvals are human actions, so
# only authenticated project members may reach it. Open it with:
#   gcloud run services proxy sre-console --region "$GCP_REGION" --port 8080
# then browse http://localhost:8080 (put IAP in front for a shared URL).
set -euo pipefail

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
: "${GCP_REGION:?set GCP_REGION}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.6-flash}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
REPO="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/sre-agent"
IMAGE="${REPO}/sre-console:$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
ENV_VARS="GCP_PROJECT_ID=${GCP_PROJECT_ID},GCP_REGION=${GCP_REGION},GEMINI_MODEL=${GEMINI_MODEL},GEMINI_LOCATION=${GEMINI_LOCATION}"

cd "$(dirname "$0")/.."
echo ">> building ${IMAGE} with Cloud Build"
gcloud builds submit --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --tag "$IMAGE" .

echo ">> deploying sre-console"
gcloud run deploy sre-console \
  --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --image "$IMAGE" \
  --service-account "sre-agent@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "$ENV_VARS" \
  --memory 2Gi --cpu 2 --timeout 900 --concurrency 20 \
  --min-instances 0 --max-instances 3 \
  --no-allow-unauthenticated

if [[ "${1:-}" == "ingest" ]]; then
  echo ">> deploying sre-ingest (streaming pull needs an always-on CPU)"
  gcloud run deploy sre-ingest \
    --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --image "$IMAGE" \
    --service-account "sre-ingest@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
    --set-env-vars "$ENV_VARS" \
    --command python --args "-m,ingest.consumer" \
    --min-instances 1 --max-instances 1 --no-cpu-throttling \
    --memory 1Gi --cpu 1 --no-allow-unauthenticated
fi

echo
echo "Open the console:"
echo "  gcloud run services proxy sre-console --region ${GCP_REGION} --port 8080   # then http://localhost:8080"
