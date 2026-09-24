#!/usr/bin/env bash
# =============================================================================
# bq_runner.sh -- Primary BigQuery SQL runner for GCP Cloud Shell / ADC setups.
# Executes standard SQL using the `bq` CLI against the active GCP project,
# and automatically falls back to `bqq.sh` (REST API runner) if `bq` fails.
#
# Usage:
#   ./tools/bq_runner.sh "SELECT 1"
#   ./tools/bq_runner.sh -f sql/01_schema_additions.sql
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FALLBACK_BQQ="${SCRIPT_DIR}/bqq.sh"

ADC_FILE="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/.config/gcloud/application_default_credentials.json}"
ADC_PROJECT=""
if [[ -f "$ADC_FILE" ]]; then
  ADC_PROJECT="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("quota_project_id") or d.get("project_id") or "")' "$ADC_FILE" 2>/dev/null || true)"
fi

PROJECT="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${ADC_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}}"
if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "ERROR: No GCP project configured. Set GCP_PROJECT_ID or run: gcloud config set project <PROJECT_ID>" >&2
  exit 1
fi

LOCATION="${BQ_LOCATION:-${GCP_REGION:-${GOOGLE_CLOUD_REGION:-${GOOGLE_CLOUD_LOCATION:-$(gcloud config get-value compute/region 2>/dev/null || true)}}}}"
if [[ -z "$LOCATION" || "$LOCATION" == "(unset)" ]]; then
  echo "ERROR: No GCP region/location configured. Set GCP_REGION (or BQ_LOCATION) or run: gcloud config set compute/region <REGION>" >&2
  exit 1
fi

export GCP_PROJECT_ID="$PROJECT"
export GCP_REGION="$LOCATION"
export BQ_LOCATION="$LOCATION"

if [[ "${1:-}" == "-f" ]]; then
  if [[ -z "${2:-}" || ! -f "${2:-}" ]]; then
    echo "ERROR: SQL file not found: '${2:-}'" >&2
    exit 1
  fi
  RAW_SQL="$(cat "$2")"
else
  RAW_SQL="${1:-}"
fi

if [[ -z "$RAW_SQL" ]]; then
  echo "ERROR: Empty SQL statement provided." >&2
  exit 1
fi

# Substitute project and location placeholders
SQL="${RAW_SQL//__PROJECT_ID__/$PROJECT}"
SQL="${SQL//__LOCATION__/$LOCATION}"
SQL="${SQL//__REGION__/$LOCATION}"

run_with_bq() {
  command -v bq >/dev/null 2>&1 || return 1
  bq version >/dev/null 2>&1 || return 1
  printf '%s\n' "$SQL" | bq --project_id="$PROJECT" --location="$LOCATION" query \
    --use_legacy_sql=false \
    --max_rows=200 \
    --format=pretty
}

# Buffer the bq attempt rather than letting it write straight to the terminal.
# bq echoes every statement it runs, so a failure part-way through a seed file
# printed the whole file plus the error payload -- and then the REST fallback
# printed all of it a second time. Roughly a thousand lines of scrollback for
# an error the fallback usually went on to handle successfully. We now show
# bq's output only when it is the answer (success), or when nothing worked.
BQ_OUTPUT_FILE="$(mktemp)"
trap 'rm -f "$BQ_OUTPUT_FILE"' EXIT

# Capture status via '|| status=$?' rather than reading $? after an if-block:
# when an if condition fails and there is no else branch, the compound command
# itself returns 0, so $? would report success for a command that just failed.
bq_status=0
run_with_bq >"$BQ_OUTPUT_FILE" 2>&1 || bq_status=$?

if [[ $bq_status -eq 0 ]]; then
  cat "$BQ_OUTPUT_FILE"
  exit 0
fi

echo "NOTE: 'bq' did not complete (exit ${bq_status}); retrying via REST runner..." >&2

if [[ ! -f "$FALLBACK_BQQ" ]]; then
  echo "ERROR: Fallback runner not found at '${FALLBACK_BQQ}'." >&2
  echo "--- output from bq ---" >&2
  cat "$BQ_OUTPUT_FILE" >&2
  exit "$bq_status"
fi

fallback_status=0
bash "$FALLBACK_BQQ" "$@" || fallback_status=$?

if [[ $fallback_status -eq 0 ]]; then
  exit 0
fi

# Both paths failed, so the buffered bq output is now diagnostically useful.
echo >&2
echo "--- earlier output from the 'bq' attempt (exit ${bq_status}) ---" >&2
cat "$BQ_OUTPUT_FILE" >&2
exit "$fallback_status"
