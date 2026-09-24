import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loader"))

import load_warehouse as lw  # noqa: E402


def test_substitute_fills_all_placeholders():
    sql = "CREATE SCHEMA `__PROJECT_ID__.x` OPTIONS(location='__LOCATION__'); -- __REGION__"
    out = lw.substitute(sql, "proj-1", "us-central1")
    assert out == "CREATE SCHEMA `proj-1.x` OPTIONS(location='us-central1'); -- us-central1"


def test_substitute_rejects_unknown_placeholder():
    with pytest.raises(lw.LoadError, match="__DATASET__"):
        lw.substitute("SELECT '__DATASET__'", "p", "l")


@pytest.mark.parametrize("path", sorted((lw.KIT_SQL_DIR).rglob("*.sql")) + sorted(lw.AGENT_OPS_SQL_DIR.glob("*.sql")),
                         ids=lambda p: p.name)
def test_every_sql_file_substitutes_cleanly(path):
    out = lw.substitute(path.read_text(encoding="utf-8"), "proj-1", "us-central1")
    assert "__PROJECT_ID__" not in out and "__LOCATION__" not in out


def test_full_plan_order():
    keys = [s.key for s in lw.plan_steps()]
    assert keys[:7] == ["01", "02", "03", "04", "05", "06", "07"]
    assert keys[7:] == ["ops:01_agent_ops_schema", "ops:02_seed_sla_policy", "ops:03_views"]


def test_seed_only_skips_ddl():
    keys = [s.key for s in lw.plan_steps(seed_only=True)]
    assert "01" not in keys and keys[0] == "02"


def test_only_filter_excludes_agent_ops_unless_requested():
    assert [s.key for s in lw.plan_steps(only={"04"})] == ["04"]
    assert [s.key for s in lw.plan_steps(only={"07", "ops"})][0] == "07"
    assert len(lw.plan_steps(only={"ops"})) == 3


def _v1_rows(**overrides):
    rows = {name: {"check_name": name, "row_count": n, "duplicate_keys": 0}
            for name, n in lw.EXPECTED_ROWS.items()}
    rows["correlated_alerts"] = {"check_name": "correlated_alerts", "row_count": 30, "duplicate_keys": 0}
    for fk in ("broken_fk_correlated_to_incidents", "broken_fk_correlated_to_alerts"):
        rows[fk] = {"check_name": fk, "row_count": 0, "duplicate_keys": 0}
    for name, patch in overrides.items():
        rows[name] = {**rows[name], **patch}
    return list(rows.values())


def test_check_tables_passes_on_expected_counts():
    assert lw.check_tables(_v1_rows()) == []


def test_check_tables_flags_problems():
    failures = lw.check_tables(_v1_rows(
        alert_stream={"row_count": 0},
        correlated_alerts={"row_count": 70},
        broken_fk_correlated_to_alerts={"row_count": 2},
        incident_postmortems={"row_count": 1},
    ))
    joined = "\n".join(failures)
    assert "alert_stream: 0 rows" in joined
    assert "correlated_alerts: 70 rows" in joined
    assert "broken_fk_correlated_to_alerts: 2" in joined
    assert "incident_postmortems: 1 rows" in joined


def test_check_embeddings():
    ok = {"runbooks": 20, "unembedded": 0, "dim_768": 20, "all_zero_vectors": 0, "duplicate_runbooks": 0}
    assert lw.check_embeddings(ok) == []
    bad = {**ok, "unembedded": 20, "dim_768": 0}
    assert any("step 04" in f for f in lw.check_embeddings(bad))


def test_retryable_embedding_errors():
    assert lw.is_retryable_embedding_error(
        "Permission denied: does not have the permission to access or use the endpoint")
    assert not lw.is_retryable_embedding_error("Syntax error: Unexpected keyword")


def test_resolve_target_env(monkeypatch):
    for k in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "BQ_LOCATION", "GCP_REGION",
              "GOOGLE_CLOUD_REGION", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.setenv("BQ_LOCATION", "northamerica-northeast1")
    assert lw.resolve_target(None, None) == ("p", "northamerica-northeast1")
    monkeypatch.delenv("GCP_PROJECT_ID")
    with pytest.raises(lw.LoadError):
        lw.resolve_target(None, None)
