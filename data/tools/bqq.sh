#!/bin/bash
# Run a single BigQuery standard SQL statement against the active GCP project using ADC.
# Uses the REST jobs.query endpoint directly -- the `bq` CLI on the hackfest
# build silently authenticates as a capsule service account and returns empty
# results, so it must NOT be used.
# Usage: ./bqq.sh "SELECT 1"   OR   ./bqq.sh -f query.sql
#
# Set BQQ_DEBUG=1 to print the full raw API error payload instead of just the
# human-readable message. The raw payloads embed server-side Java stack traces
# that run to thousands of characters and bury the one line that matters.
set -euo pipefail
PROJECT="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "ERROR: No GCP project configured. Set GCP_PROJECT_ID or run: gcloud config set project <PROJECT_ID>" >&2
  exit 1
fi
LOCATION="${BQ_LOCATION:-${GCP_REGION:-${GOOGLE_CLOUD_REGION:-${GOOGLE_CLOUD_LOCATION:-$(gcloud config get-value compute/region 2>/dev/null || true)}}}}"
if [ "${1:-}" = "-f" ]; then SQL=$(cat "$2"); else SQL="$1"; fi
TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token)
python3 - "$PROJECT" "$LOCATION" "$SQL" "$TOKEN" <<'PYEOF'
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
project, location, sql, token = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sql = sql.replace("__PROJECT_ID__", project)
if location and location != "(unset)":
    sql = sql.replace("__LOCATION__", location).replace("__REGION__", location)
else:
    location = ""

DEBUG = os.environ.get("BQQ_DEBUG", "") not in ("", "0", "false")


def die(raw, context=""):
    """Print the useful part of a BigQuery error and exit non-zero.

    BigQuery error payloads nest the same message several times and append a
    server-side stack trace in debugInfo. Printing the lot pushes the actual
    cause off the top of the terminal, so by default we surface only the
    top-level message and reason.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode(errors="replace")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            print(f"ERROR{context}: {raw[:1000]}", file=sys.stderr)
            sys.exit(1)
    else:
        parsed = raw

    if DEBUG:
        print(json.dumps(parsed, indent=2), file=sys.stderr)
        sys.exit(1)

    err = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
    if isinstance(err, list):
        err = err[0] if err else {}

    message = err.get("message") if isinstance(err, dict) else None
    reason = err.get("reason") if isinstance(err, dict) else None
    if not message:
        message = json.dumps(parsed)[:1000]

    print(f"ERROR{context}: {message}", file=sys.stderr)
    if reason:
        print(f"  reason: {reason}", file=sys.stderr)
    print("  (set BQQ_DEBUG=1 for the full API payload)", file=sys.stderr)
    sys.exit(1)


payload = {
    "query": sql,
    "useLegacySql": False,
    "maxResults": 200,
    "timeoutMs": 30000,
}
if location:
    payload["location"] = location

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "x-goog-user-project": project,
}
req = urllib.request.Request(
    f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries",
    data=json.dumps(payload).encode(),
    headers=headers,
)
try:
    resp = json.load(urllib.request.urlopen(req))
except urllib.error.HTTPError as e:
    die(e.read(), f" (HTTP {e.code})")
if "error" in resp:
    die(resp)

job_ref = resp.get("jobReference", {})
job_id = job_ref.get("jobId")
job_loc = job_ref.get("location") or location or "US"

while not resp.get("jobComplete", False) and job_id:
    time.sleep(2)
    poll_url = (
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries/{urllib.parse.quote(job_id)}"
        f"?location={urllib.parse.quote(job_loc)}&maxResults=200&timeoutMs=30000"
    )
    poll_req = urllib.request.Request(poll_url, headers=headers)
    try:
        resp = json.load(urllib.request.urlopen(poll_req))
    except urllib.error.HTTPError as e:
        die(e.read(), f" (HTTP {e.code})")
    if "error" in resp:
        die(resp)

if "errors" in resp and resp["errors"]:
    die({"error": resp["errors"][0]})

fields = [f["name"] for f in resp.get("schema", {}).get("fields", [])]
rows = resp.get("rows", [])
if fields:
    print("\t".join(fields))
    print("-" * 80)
    for r in rows:
        print("\t".join("" if c["v"] is None else str(c["v"]) for c in r["f"]))
    print(f"\n({resp.get('totalRows','?')} total rows)")
else:
    print("(statement complete)")
PYEOF
