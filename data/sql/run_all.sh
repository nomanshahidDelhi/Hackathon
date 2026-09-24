#!/usr/bin/env bash
# =============================================================================
# BCE Hackfest 2026 -- Track 2: Enterprise Observability & Support
# run_all.sh -- execute the numbered seed SQL files in order.
# =============================================================================
# WHY THIS EXISTS
#   The `bq` CLI on the hackfest image silently authenticates as a capsule
#   service account and returns empty result sets with no error. Every
#   statement therefore has to go through bqq.sh, which hits the REST
#   jobs.query endpoint with the operator's own ADC token. This wrapper is the
#   only supported way to (re-)seed the demo.
#
# WHY --seed-only
#   On event day the data gets re-rolled several times (once at 09:00, then
#   after each twist). Re-running the DDL is pointless at best and, if a team
#   has added columns to their own copy of a table, actively destructive.
#   --seed-only re-runs just the data generators, which are all
#   CREATE OR REPLACE and therefore idempotent.
#
# WHY MISSING FILES ARE ONLY A WARNING
#   The seed set is authored by several people in parallel. A partially
#   populated sql/ directory is a normal intermediate state, not an error --
#   you should still be able to re-roll telemetry while runbooks are in flight.
#
# Usage:
#   ./run_all.sh                 # everything, DDL first
#   ./run_all.sh --seed-only     # skip schema/DDL, re-run data generators only
#   ./run_all.sh --only 05,06    # run just these numbered steps
#   ./run_all.sh --list          # show what would run, execute nothing
#   ./run_all.sh --verify-only   # run only the verify/ checks
#
# Env:
#   Project and region are read from the environment first, then from gcloud
#   config. Both are printed in the banner before anything runs -- check them.
#   Every .sql file is templated: __PROJECT_ID__ and __LOCATION__ are
#   substituted from these two values by the runner.
#
#   GCP_PROJECT_ID=<project-id>  # target project. Falls back to
#                                #   GOOGLE_CLOUD_PROJECT, then the ADC quota
#                                #   project, then `gcloud config get-value project`
#   GCP_REGION=<region>          # BigQuery dataset location, e.g. us-central1
#                                #   or northamerica-northeast1. Alias:
#                                #   BQ_LOCATION (takes precedence). Falls back
#                                #   to GOOGLE_CLOUD_REGION, GOOGLE_CLOUD_LOCATION,
#                                #   then `gcloud config get-value compute/region`
#   BQQ=/path/to/bq_runner.sh    # override the query runner
#
#   Example:
#     export GCP_PROJECT_ID="my-team-project"
#     export GCP_REGION="us-central1"
#     ./run_all.sh
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the query runner: explicit BQQ override first, then the repo-local
# copy. There is deliberately no absolute fallback -- an earlier version
# pointed at one operator's personal scratch directory, which works on exactly
# one machine and fails confusingly everywhere else.
BQQ="${BQQ:-}"
if [[ -z "$BQQ" ]]; then
  if [[ -f "$SCRIPT_DIR/../tools/bq_runner.sh" ]]; then
    BQQ="$SCRIPT_DIR/../tools/bq_runner.sh"
  elif [[ -f "$SCRIPT_DIR/../tools/bqq.sh" ]]; then
    BQQ="$SCRIPT_DIR/../tools/bqq.sh"
  fi
fi

# -----------------------------------------------------------------------------
# The pipeline, by step number. Files are resolved by numeric prefix glob
# (NN_*.sql) rather than exact filename, so a co-author renaming their step
# does not break this script.
#
#   kind=ddl   -> schema / structural changes, skipped by --seed-only
#   kind=seed  -> data generation, always idempotent (CREATE IF NOT EXISTS + MERGE)
# -----------------------------------------------------------------------------
STEPS=(
  "01|ddl |datasets & schema additions"
  "02|seed|customer accounts (45 accounts)"
  "03|seed|runbooks (20 SOPs)"
  "04|seed|runbook embeddings (model + 768-dim vectors)"
  "05|seed|network topology (64 nodes)"
  "06|seed|alert stream telemetry (3,000 alerts)"
  "07|seed|incident mart (18 incidents, correlations, remediation logs)"
)

SEED_ONLY=0
LIST_ONLY=0
SKIP_VERIFY=0
VERIFY_ONLY=0
ONLY_FILTER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed-only)   SEED_ONLY=1; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --list)        LIST_ONLY=1; shift ;;
    --only)        ONLY_FILTER="${2:-}"; shift 2 ;;
    --only=*)      ONLY_FILTER="${1#*=}"; shift ;;
    -h|--help)     sed -n '1,52p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "run_all.sh: unknown argument '$1' (try --help)" >&2; exit 2 ;;
  esac
done

ADC_FILE="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/.config/gcloud/application_default_credentials.json}"
ADC_PROJECT=""
if [[ -f "$ADC_FILE" ]]; then
  ADC_PROJECT="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("quota_project_id") or d.get("project_id") or "")' "$ADC_FILE" 2>/dev/null || true)"
fi

export GCP_PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${ADC_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}}"
if [[ -z "$GCP_PROJECT_ID" || "$GCP_PROJECT_ID" == "(unset)" ]]; then
  echo "FATAL: No GCP project configured." >&2
  echo "Run: export GCP_PROJECT_ID=<your-project-id>  OR  gcloud config set project <your-project-id>" >&2
  exit 1
fi

export BQ_LOCATION="${BQ_LOCATION:-${GCP_REGION:-${GOOGLE_CLOUD_REGION:-${GOOGLE_CLOUD_LOCATION:-$(gcloud config get-value compute/region 2>/dev/null || true)}}}}"
if [[ -z "$BQ_LOCATION" || "$BQ_LOCATION" == "(unset)" ]]; then
  echo "FATAL: No GCP region/location configured." >&2
  echo "Run: export GCP_REGION=<your-region> (e.g. northamerica-northeast1 or us-central1)  OR  gcloud config set compute/region <your-region>" >&2
  exit 1
fi
export GCP_REGION="$BQ_LOCATION"

if [[ ! -x "$BQQ" ]]; then
  if [[ -f "$BQQ" ]]; then
    chmod +x "$BQQ" 2>/dev/null || true
  fi
fi
if [[ ! -f "$BQQ" ]]; then
  echo "FATAL: query runner not found at '$BQQ'. Set BQQ=/path/to/bq_runner.sh." >&2
  exit 1
fi

ensure_project_prereqs() {
  echo
  echo ">> [00] Ensuring APIs & BigQuery->Vertex connection (${BQ_LOCATION}.vertex_conn) on ${GCP_PROJECT_ID}"
  gcloud services enable \
    bigquery.googleapis.com \
    bigqueryconnection.googleapis.com \
    aiplatform.googleapis.com \
    --project "${GCP_PROJECT_ID}" >/dev/null

  local conn_sa=""
  if command -v bq >/dev/null 2>&1; then
    if ! bq show --connection --project_id="${GCP_PROJECT_ID}" --location="${BQ_LOCATION}" vertex_conn >/dev/null 2>&1; then
      bq mk --connection \
        --connection_type=CLOUD_RESOURCE \
        --project_id="${GCP_PROJECT_ID}" \
        --location="${BQ_LOCATION}" \
        vertex_conn >/dev/null 2>&1 || true
    fi
    conn_sa="$(bq show --format=prettyjson --connection --project_id="${GCP_PROJECT_ID}" --location="${BQ_LOCATION}" vertex_conn 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("cloudResource",{}).get("serviceAccountId",""))' 2>/dev/null || true)"
  fi

  if [[ -z "${conn_sa}" ]]; then
    local token
    token="$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token)"
    conn_sa="$(python3 - "${GCP_PROJECT_ID}" "${BQ_LOCATION}" "${token}" <<'PYEOF'
import json, sys, urllib.error, urllib.request

project, location, token = sys.argv[1], sys.argv[2], sys.argv[3]
base = f"https://bigqueryconnection.googleapis.com/v1/projects/{project}/locations/{location}/connections"
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

try:
    req = urllib.request.Request(f"{base}/vertex_conn", headers=headers)
    resp = json.load(urllib.request.urlopen(req))
except urllib.error.HTTPError as e:
    if e.code == 404:
        body = json.dumps({"cloudResource": {}}).encode()
        req = urllib.request.Request(f"{base}?connectionId=vertex_conn", data=body, headers=headers, method="POST")
        resp = json.load(urllib.request.urlopen(req))
    else:
        sys.stderr.write(f"Connection API HTTP {e.code}: {e.read().decode()[:500]}\n")
        sys.exit(1)

print(resp.get("cloudResource", {}).get("serviceAccountId", ""))
PYEOF
    )"
  fi

  if [[ -n "${conn_sa}" ]]; then
    echo "   Connection SA: ${conn_sa}"
    # The echo below used to run unconditionally. On a project where the caller
    # lacks resourcemanager.projects.setIamPolicy that reported a grant that had
    # not happened, and the run then failed much later at step 04 on a
    # permission the operator had just been told they had.
    if gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
         --member="serviceAccount:${conn_sa}" \
         --role="roles/aiplatform.user" \
         --condition=None \
         --quiet >/dev/null 2>&1; then
      echo "   Granted roles/aiplatform.user to ${conn_sa}"
    else
      echo "   WARNING: could not grant roles/aiplatform.user to ${conn_sa}." >&2
      echo "   You need roles/resourcemanager.projectIamAdmin (or Owner) on ${GCP_PROJECT_ID}." >&2
      echo "   Ask a project admin to run:" >&2
      echo "     gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \\" >&2
      echo "       --member=serviceAccount:${conn_sa} --role=roles/aiplatform.user" >&2
      echo "   Step 04 (embeddings) will fail until this is done." >&2
    fi
  else
    echo "   WARNING: could not determine the vertex_conn service account." >&2
    echo "   Step 04 (embeddings) will likely fail. Check that the BigQuery" >&2
    echo "   Connection API is enabled and that you are authenticated." >&2
  fi
}

echo "=============================================================="
echo " BCE Hackfest 2026 / Track 2 -- seed pipeline"
echo " project  : $GCP_PROJECT_ID"
echo " location : $BQ_LOCATION"
echo " sql dir  : $SCRIPT_DIR"
echo " runner   : $BQQ"
echo " mode     : $( [[ $SEED_ONLY -eq 1 ]] && echo 'seed-only (DDL skipped)' || echo 'full' )"
[[ -n "$ONLY_FILTER" ]] && echo " filter   : steps $ONLY_FILTER"
echo "=============================================================="

if [[ $LIST_ONLY -eq 0 && $VERIFY_ONLY -eq 0 ]]; then
  ensure_project_prereqs
fi

RAN=0; SKIPPED=0; MISSING=0; FAILED=0; VERIFIED=0
FAILED_STEPS=()

if [[ $VERIFY_ONLY -eq 0 ]]; then
  for entry in "${STEPS[@]}"; do
    IFS='|' read -r num kind desc <<< "$entry"
    kind="${kind// /}"

    if [[ -n "$ONLY_FILTER" && ",$ONLY_FILTER," != *",$num,"* ]]; then
      continue
    fi

    if [[ $SEED_ONLY -eq 1 && "$kind" == "ddl" ]]; then
      echo "-- [$num] SKIP (ddl, --seed-only): $desc"
      SKIPPED=$((SKIPPED+1))
      continue
    fi

    # Resolve by numeric prefix so co-authored filenames can drift.
    shopt -s nullglob
    matches=("$SCRIPT_DIR/${num}_"*.sql)
    shopt -u nullglob

    if [[ ${#matches[@]} -eq 0 ]]; then
      echo "!! [$num] WARNING: no file matching '${num}_*.sql' ($desc) -- skipping."
      MISSING=$((MISSING+1))
      continue
    fi
    if [[ ${#matches[@]} -gt 1 ]]; then
      echo "!! [$num] WARNING: ${#matches[@]} files match '${num}_*.sql'; running all of them."
    fi

    for f in "${matches[@]}"; do
      if [[ $LIST_ONLY -eq 1 ]]; then
        echo ">> [$num] would run $(basename "$f")  ($desc)"
        continue
      fi
      echo
      echo ">> [$num] $(basename "$f")  --  $desc"
      start=$SECONDS
      max_attempts=1
      [[ "$num" == "04" ]] && max_attempts=8

      attempt=1
      step_ok=0
      while [[ $attempt -le $max_attempts ]]; do
        if "$BQQ" -f "$f"; then
          step_ok=1
          break
        fi
        if [[ $attempt -lt $max_attempts ]]; then
          echo "   [attempt $attempt/$max_attempts] waiting 20s for Vertex IAM propagation before retrying..."
          sleep 20
        fi
        attempt=$((attempt+1))
      done

      if [[ $step_ok -eq 1 ]]; then
        echo "   OK in $((SECONDS-start))s"
        RAN=$((RAN+1))
      else
        echo "   FAILED after $((SECONDS-start))s"
        FAILED=$((FAILED+1))
        FAILED_STEPS+=("$num:$(basename "$f")")
      fi
    done
  done
fi

if [[ $SKIP_VERIFY -eq 0 && $FAILED -eq 0 ]]; then
  shopt -s nullglob
  verify_files=("$SCRIPT_DIR/verify/v"*.sql)
  shopt -u nullglob

  if [[ ${#verify_files[@]} -gt 0 ]]; then
    echo
    echo "=============================================================="
    echo " Running Final Verification Checks (${#verify_files[@]} scripts)"
    echo "=============================================================="
    for vf in "${verify_files[@]}"; do
      vname="$(basename "$vf")"
      if [[ $LIST_ONLY -eq 1 ]]; then
        echo ">> [verify] would run verify/$vname"
        continue
      fi
      echo
      echo ">> [verify] verify/$vname"
      vstart=$SECONDS
      if "$BQQ" -f "$vf"; then
        echo "   OK in $((SECONDS-vstart))s"
        VERIFIED=$((VERIFIED+1))
      else
        echo "   FAILED after $((SECONDS-vstart))s"
        FAILED=$((FAILED+1))
        FAILED_STEPS+=("verify:$vname")
      fi
    done
  fi
fi

echo
echo "=============================================================="
echo " ran=$RAN  verified=$VERIFIED  skipped=$SKIPPED  missing=$MISSING  failed=$FAILED"
if [[ $FAILED -gt 0 ]]; then
  echo " failures: ${FAILED_STEPS[*]}"
  echo "=============================================================="
  exit 1
fi
echo "=============================================================="
exit 0
