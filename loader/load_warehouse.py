#!/usr/bin/env python3
"""Load the Track 2 warehouse kit (data/sql) plus our own sre_agent_ops dataset into BigQuery.

Re-runnable: every kit step is CREATE ... IF NOT EXISTS or MERGE, and so is
every agent_ops step, so a rerun repairs a partial load instead of duplicating it.

Usage:
    python loader/load_warehouse.py                  # full load + verify
    python loader/load_warehouse.py --seed-only      # re-roll data (skip DDL step 01)
    python loader/load_warehouse.py --only 06,07     # just these kit steps
    python loader/load_warehouse.py --verify-only    # definition-of-done checks
    python loader/load_warehouse.py --list           # show the plan, execute nothing
    python loader/load_warehouse.py --render out/    # write substituted SQL, execute nothing

Project/location come from --project/--location, else GCP_PROJECT_ID/GCP_REGION
(BQ_LOCATION takes precedence for location), else GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_REGION.
Queries run through the Python client with Application Default Credentials, not
the `bq` CLI (which on the lab image can silently use the wrong identity).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_SQL_DIR = REPO_ROOT / "data" / "sql"
AGENT_OPS_SQL_DIR = REPO_ROOT / "sql" / "agent_ops"

KIT_DATASETS = ("sre_telemetry", "sre_topology", "sre_knowledge_base", "sre_incident_mart")
AGENT_DATASET = "sre_agent_ops"

# Kit steps in execution order. 07 reads what 05 and 06 wrote; 04 needs vertex_conn.
KIT_STEPS = (
    ("01", "ddl", "datasets & declared-schema tables"),
    ("02", "seed", "customer accounts"),
    ("03", "seed", "runbooks"),
    ("04", "seed", "runbook embeddings (remote model + 768-dim vectors)"),
    ("05", "seed", "network topology"),
    ("06", "seed", "alert stream telemetry"),
    ("07", "seed", "incident mart"),
)
EMBEDDING_STEP = "04"

# Definition of done, from the Assets Guide ("You are done when").
EXPECTED_ROWS = {
    "alert_stream": 3000,
    "network_nodes": 64,
    "runbooks": 20,
    "customer_accounts": 45,
    "incidents": 18,
    "remediation_logs": 18,
    "incident_postmortems": 0,
}
CORRELATED_ALERTS_RANGE = (15, 55)
EMBEDDING_DIM = 768

PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z_]*__")


class LoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    key: str        # "01".."07" for kit steps, "ops:<file>" for ours
    kind: str       # ddl | seed | agent_ops
    desc: str
    path: Path


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without GCP)
# ---------------------------------------------------------------------------
def substitute(sql: str, project: str, location: str) -> str:
    """Fill the kit placeholders and refuse to return SQL with any left over."""
    out = (
        sql.replace("__PROJECT_ID__", project)
        .replace("__LOCATION__", location)
        .replace("__REGION__", location)
    )
    leftover = sorted(set(PLACEHOLDER_RE.findall(out)))
    if leftover:
        raise LoadError(f"unsubstituted placeholders: {', '.join(leftover)}")
    return out


def plan_steps(
    kit_dir: Path = KIT_SQL_DIR,
    ops_dir: Path = AGENT_OPS_SQL_DIR,
    seed_only: bool = False,
    only: set[str] | None = None,
    include_agent_ops: bool = True,
) -> list[Step]:
    """Resolve the ordered list of SQL files to run.

    --only restricts kit steps and skips agent_ops unless "ops" is listed.
    """
    steps: list[Step] = []
    for num, kind, desc in KIT_STEPS:
        if only is not None and num not in only:
            continue
        if seed_only and kind == "ddl":
            continue
        matches = sorted(kit_dir.glob(f"{num}_*.sql"))
        if len(matches) != 1:
            raise LoadError(f"expected exactly one {num}_*.sql in {kit_dir}, found {len(matches)}")
        steps.append(Step(num, kind, desc, matches[0]))

    if include_agent_ops and (only is None or "ops" in only):
        for path in sorted(ops_dir.glob("*.sql")):
            steps.append(Step(f"ops:{path.stem}", "agent_ops", "sre_agent_ops", path))
    return steps


def check_tables(rows: list[dict]) -> list[str]:
    """Evaluate verify/v1_tables.sql output. Returns a list of failures."""
    by_name = {r["check_name"]: r for r in rows}
    failures: list[str] = []
    for name, expected in EXPECTED_ROWS.items():
        row = by_name.get(name)
        if row is None:
            failures.append(f"{name}: missing from verify output")
            continue
        if row["row_count"] != expected:
            failures.append(f"{name}: {row['row_count']} rows, expected {expected}")
        if row["duplicate_keys"]:
            failures.append(f"{name}: {row['duplicate_keys']} duplicate keys")

    corr = by_name.get("correlated_alerts")
    lo, hi = CORRELATED_ALERTS_RANGE
    if corr is None:
        failures.append("correlated_alerts: missing from verify output")
    else:
        if not lo <= corr["row_count"] <= hi:
            failures.append(f"correlated_alerts: {corr['row_count']} rows, expected {lo}-{hi}")
        if corr["duplicate_keys"]:
            failures.append(f"correlated_alerts: {corr['duplicate_keys']} duplicate keys")

    for name in ("broken_fk_correlated_to_incidents", "broken_fk_correlated_to_alerts"):
        row = by_name.get(name)
        if row is None:
            failures.append(f"{name}: missing from verify output")
        elif row["row_count"]:
            failures.append(f"{name}: {row['row_count']} broken keys")
    return failures


def check_embeddings(row: dict) -> list[str]:
    """Evaluate verify/v4_embedding_dims.sql output. Returns a list of failures."""
    failures: list[str] = []
    n = EXPECTED_ROWS["runbooks"]
    if row["runbooks"] != n:
        failures.append(f"runbooks: {row['runbooks']}, expected {n}")
    if row["unembedded"]:
        failures.append(f"unembedded runbooks: {row['unembedded']} (step 04 did not finish; re-run it)")
    if row["dim_768"] != n:
        failures.append(f"runbooks with {EMBEDDING_DIM}-dim vectors: {row['dim_768']}, expected {n}")
    if row["all_zero_vectors"]:
        failures.append(f"all-zero vectors: {row['all_zero_vectors']}")
    if row["duplicate_runbooks"]:
        failures.append(f"duplicate runbooks: {row['duplicate_runbooks']}")
    return failures


def is_retryable_embedding_error(message: str) -> bool:
    """Step 04 fails while the vertex_conn IAM grant propagates: retry, don't redesign."""
    m = message.lower()
    return (
        "permission" in m
        or "does not have" in m
        or "access denied" in m
        or "service agent" in m
        or "try again" in m
        or "rate limit" in m
        or "quota" in m
    )


def resolve_target(project: str | None, location: str | None) -> tuple[str, str]:
    env = os.environ
    project = project or env.get("GCP_PROJECT_ID") or env.get("GOOGLE_CLOUD_PROJECT")
    location = (
        location
        or env.get("BQ_LOCATION")
        or env.get("GCP_REGION")
        or env.get("GOOGLE_CLOUD_REGION")
        or env.get("GOOGLE_CLOUD_LOCATION")
    )
    if not project:
        raise LoadError("no project: pass --project or set GCP_PROJECT_ID")
    if not location:
        raise LoadError("no location: pass --location or set GCP_REGION (e.g. us-central1)")
    return project, location


# ---------------------------------------------------------------------------
# BigQuery side
# ---------------------------------------------------------------------------
class Warehouse:
    def __init__(self, project: str, location: str):
        from google.cloud import bigquery  # imported lazily so --list/--render/tests need no GCP libs

        self.bigquery = bigquery
        self.project = project
        self.location = location
        self.client = bigquery.Client(project=project, location=location)

    def describe_identity(self) -> str:
        import google.auth

        creds, adc_project = google.auth.default()
        who = getattr(creds, "service_account_email", None) or f"user credentials ({type(creds).__name__})"
        return f"{who}; ADC project={adc_project or '-'}"

    def check_dataset_locations(self) -> None:
        from google.api_core.exceptions import NotFound

        for ds in (*KIT_DATASETS, AGENT_DATASET):
            try:
                existing = self.client.get_dataset(f"{self.project}.{ds}")
            except NotFound:
                continue
            if existing.location.lower() != self.location.lower():
                raise LoadError(
                    f"dataset {ds} already exists in {existing.location}, not {self.location}. "
                    f"Stay on {existing.location} or drop the datasets first."
                )

    def check_vertex_conn(self) -> None:
        try:
            from google.cloud import bigquery_connection_v1
        except ImportError:
            print("   (google-cloud-bigquery-connection not installed; skipping vertex_conn pre-check)")
            return
        from google.api_core.exceptions import NotFound

        name = f"projects/{self.project}/locations/{self.location}/connections/vertex_conn"
        try:
            conn = bigquery_connection_v1.ConnectionServiceClient().get_connection(name=name)
        except NotFound:
            raise LoadError(
                f"connection vertex_conn not found in {self.location}. "
                "Run `terraform apply` in infra/terraform first (same region as the datasets)."
            ) from None
        print(f"   vertex_conn service agent: {conn.cloud_resource.service_account_id}")

    def run(self, sql: str, timeout: float = 900) -> list[dict]:
        job = self.client.query(sql, location=self.location)
        result = job.result(timeout=timeout)
        return [dict(row.items()) for row in result]


def run_step(wh: Warehouse, step: Step, attempts: int, backoff: float) -> None:
    from google.api_core.exceptions import GoogleAPICallError

    sql = substitute(step.path.read_text(encoding="utf-8"), wh.project, wh.location)
    max_attempts = attempts if step.key == EMBEDDING_STEP else 1
    for attempt in range(1, max_attempts + 1):
        try:
            wh.run(sql)
            return
        except GoogleAPICallError as exc:
            msg = str(exc)
            if "not found: connection" in msg.lower():
                raise LoadError(
                    "vertex_conn missing or in another region: create it (terraform apply) "
                    f"in {wh.location}, then re-run --only 04"
                ) from exc
            if attempt < max_attempts and is_retryable_embedding_error(msg):
                print(f"   attempt {attempt}/{max_attempts} failed (IAM likely still propagating); "
                      f"retrying in {backoff:.0f}s")
                time.sleep(backoff)
                continue
            raise LoadError(f"step {step.key} failed: {msg.splitlines()[0][:500]}") from exc


def verify(wh: Warehouse) -> list[str]:
    verify_dir = KIT_SQL_DIR / "verify"
    v1 = wh.run(substitute((verify_dir / "v1_tables.sql").read_text(encoding="utf-8"), wh.project, wh.location))
    v4 = wh.run(substitute((verify_dir / "v4_embedding_dims.sql").read_text(encoding="utf-8"), wh.project, wh.location))

    print("\n   check_name                              rows  dup")
    for r in sorted(v1, key=lambda r: r["check_name"]):
        print(f"   {r['check_name']:<38}{r['row_count']:>6}{r['duplicate_keys']:>5}")
    e = v4[0]
    print(f"   runbooks={e['runbooks']} dim_768={e['dim_768']} unembedded={e['unembedded']} "
          f"min_dim={e['min_dim']} max_dim={e['max_dim']} all_zero={e['all_zero_vectors']}")

    failures = check_tables(v1) + check_embeddings(e)

    from google.api_core.exceptions import NotFound

    try:
        policy = wh.run(f"SELECT COUNT(DISTINCT tier) AS n FROM `{wh.project}.{AGENT_DATASET}.sla_policy`")
    except NotFound:
        failures.append("sla_policy: missing (load without --no-agent-ops)")
    else:
        if policy[0]["n"] != 3:
            failures.append(f"sla_policy: {policy[0]['n']} tiers, expected 3")
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project")
    p.add_argument("--location")
    p.add_argument("--seed-only", action="store_true", help="skip DDL step 01")
    p.add_argument("--only", help="comma-separated kit steps (e.g. 04 or 06,07); add 'ops' for sre_agent_ops")
    p.add_argument("--no-agent-ops", action="store_true", help="load only the kit")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--list", action="store_true", help="print the plan and exit")
    p.add_argument("--render", metavar="DIR", help="write substituted SQL to DIR and exit")
    p.add_argument("--embed-attempts", type=int, default=10)
    p.add_argument("--embed-backoff", type=float, default=30.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    try:
        steps = [] if args.verify_only else plan_steps(
            seed_only=args.seed_only, only=only, include_agent_ops=not args.no_agent_ops
        )

        if args.list:
            for s in steps:
                print(f"{s.key:<28} {s.kind:<9} {s.path.relative_to(REPO_ROOT)}  ({s.desc})")
            return 0

        project, location = resolve_target(args.project, args.location)

        if args.render:
            out = Path(args.render)
            out.mkdir(parents=True, exist_ok=True)
            for s in steps:
                (out / s.path.name).write_text(
                    substitute(s.path.read_text(encoding="utf-8"), project, location), encoding="utf-8"
                )
            print(f"rendered {len(steps)} files to {out}")
            return 0

        print("=" * 64)
        print(f" project  : {project}")
        print(f" location : {location}")
        wh = Warehouse(project, location)
        print(f" identity : {wh.describe_identity()}")
        print("=" * 64)

        wh.check_dataset_locations()
        if any(s.key == EMBEDDING_STEP for s in steps):
            wh.check_vertex_conn()

        for s in steps:
            print(f">> [{s.key}] {s.path.name} -- {s.desc}")
            t0 = time.monotonic()
            run_step(wh, s, args.embed_attempts, args.embed_backoff)
            print(f"   OK in {time.monotonic() - t0:.1f}s")

        if args.skip_verify:
            return 0

        print("\n>> verify")
        failures = verify(wh)
        if failures:
            print("\nVERIFY FAILED:")
            for f in failures:
                print(f"  - {f}")
            print("If counts are 0 with no error, check the identity above (wrong ADC account).")
            return 1
        print("\nVERIFY OK: warehouse matches the definition of done.")
        return 0

    except LoadError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
