"""Test-only: parse the 20 runbooks out of data/sql/03_seed_runbooks.sql."""
from __future__ import annotations

import re
from pathlib import Path

from agent.app.tools.retrieval import Runbook

KIT = Path(__file__).resolve().parents[2] / "data" / "sql" / "03_seed_runbooks.sql"

_RX = re.compile(
    r"\(\s*'(sop-\d+)',\s*'([^']*)',\s*'([^']*)',\s*'''(.*?)''',\s*'''(.*?)''',\s*'''(.*?)'''\s*\)",
    re.S,
)


def _unescape(s: str) -> str:
    # BigQuery string literal escapes used in the kit.
    return s.replace("\\\\", "\\")


def load_runbooks() -> list[Runbook]:
    text = KIT.read_text(encoding="utf-8")
    return [Runbook(rid, title, sig, _unescape(steps), _unescape(rb), _unescape(script))
            for rid, title, sig, steps, rb, script in _RX.findall(text)]
