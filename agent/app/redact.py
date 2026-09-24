"""Scrub credentials and personal data from any text leaving the system.

Applied to: embedding/LLM inputs and outputs, scripts stored in approvals and
remediation_logs, sandbox output, and (Phase 3) logs, traces and API/AG-UI
responses. Replacement tokens say what was removed, never the value.
"""
from __future__ import annotations

import re
from typing import Any

# Order matters: specific credential shapes before generic ones.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("SA_KEY_JSON", re.compile(r'"private_key"\s*:\s*"[^"]*"')),
    ("GCP_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[0-9A-Za-z_]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}\b")),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("OAUTH_TOKEN", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    ("JWT", re.compile(r"\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\b")),
    ("BEARER", re.compile(r"(?i)(\bbearer\s+)[0-9A-Za-z._\-~+/]{16,}=*")),
    ("URI_PASSWORD", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s]+(@)")),
    ("SECRET_ASSIGNMENT", re.compile(
        r"(?i)\b([A-Z0-9_]*?(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)"
        r"[A-Z0-9_]*\s*[=:]\s*)(['\"]?)(?!\$|\*\*\*|\[REDACTED)[^\s'\";&|]{4,}\2")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("SIN", re.compile(r"\b\d{3}[- ]\d{3}[- ]\d{3}\b")),
    ("PHONE", re.compile(r"(?<![\d.])(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?![\d.])")),
    ("CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
]


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _sub(label: str, m: re.Match[str]) -> str:
    if label == "CARD":
        digits = re.sub(r"\D", "", m.group(0))
        if not (13 <= len(digits) <= 19 and _luhn_ok(digits)):
            return m.group(0)
    if label in ("BEARER", "URI_PASSWORD"):
        return f"{m.group(1)}[REDACTED:{label}]" + (m.group(2) if label == "URI_PASSWORD" else "")
    if label == "SECRET_ASSIGNMENT":
        return f"{m.group(1)}[REDACTED:SECRET]"
    return f"[REDACTED:{label}]"


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for label, rx in _PATTERNS:
        text = rx.sub(lambda m, label=label: _sub(label, m), text)
    return text


def redact_obj(obj: Any) -> Any:
    """Redact every string inside nested dicts/lists."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def find_secrets(text: str) -> list[str]:
    """Labels of credential-like content (used by guardrails to block scripts)."""
    labels = []
    for label, rx in _PATTERNS:
        if label in ("EMAIL", "PHONE", "SIN", "CARD"):
            continue
        if rx.search(text or ""):
            labels.append(label)
    return labels
