"""Find the runbook for an incident's *root cause* (M2), by meaning.

Retrieval chain -- each tier is used only if the one before it fails:
  1. BigQuery VECTOR_SEARCH, query embedded in-warehouse with
     AI.GENERATE_EMBEDDING (text-embedding-005, RETRIEVAL_QUERY, 768 dims).
  2. In-process cosine over the 20 stored vectors, query embedded via Vertex AI.
  3. Keyword match on failure_signature/title (no model needed).
Then a deterministic re-rank on evidence: semantic similarity, how well the
failure_signature matches the root alert, a penalty when it matches a *symptom*
alert instead, and the runbook's success history in remediation_logs.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..models import Incident
from ..redact import redact

log = logging.getLogger(__name__)

RERANK_WEIGHTS = {"similarity": 0.55, "signature": 0.35, "history": 0.10, "symptom_penalty": -0.25}


@dataclass
class Runbook:
    runbook_id: str
    title: str
    failure_signature: str
    remediation_steps: str
    rollback_commands: str
    remediation_script: str
    embedding: list[float] = field(default_factory=list, repr=False)


@dataclass
class RunbookHit:
    runbook_id: str
    title: str
    failure_signature: str
    similarity: float
    method: str                    # vector_search | local_cosine | keyword
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    query: str
    method: str
    hits: list[RunbookHit]
    errors: list[str]

    @property
    def best(self) -> RunbookHit | None:
        return self.hits[0] if self.hits else None


class RunbookSource(Protocol):
    """What retrieval needs from the warehouse (Warehouse implements it; tests fake it)."""
    def vector_search(self, query: str, top_k: int) -> list[tuple[str, float]]: ...
    def fetch_runbooks(self) -> list[Runbook]: ...
    def runbook_history(self) -> dict[str, tuple[int, int]]: ...


# ---------------------------------------------------------------------------
def query_text(incident: Incident) -> str:
    """Describe the root cause (never the loudest symptom), phrased like the indexed documents."""
    rc = incident.root_cause
    return redact(
        f"{rc.alert_type.replace('_', ' ')} on {rc.service_name} ({rc.node_type}). "
        f"Failure signature: {rc.alert_type}. {rc.sample_message}"
    )


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def signature_match(signature: str, alert_type: str) -> float:
    sig, alert = tokens(signature), tokens(alert_type)
    if not sig or not alert:
        return 0.0
    if signature.split()[0] == alert_type:
        return 1.0
    return len(sig & alert) / len(sig | alert)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
def retrieve(
    incident: Incident,
    source: RunbookSource,
    embed: Callable[[str], list[float]] | None = None,
    top_k: int = 5,
    disable: set[str] = frozenset(),
) -> RetrievalResult:
    q = query_text(incident)
    errors: list[str] = []
    runbooks: list[Runbook] | None = None

    def load_runbooks() -> list[Runbook]:
        nonlocal runbooks
        if runbooks is None:
            runbooks = source.fetch_runbooks()
        return runbooks

    hits: list[RunbookHit] = []
    method = "none"

    if "vector_search" not in disable:
        try:
            meta = {r.runbook_id: r for r in load_runbooks()}
            for rid, dist in source.vector_search(q, top_k):
                r = meta[rid]
                hits.append(RunbookHit(rid, r.title, r.failure_signature, 1.0 - dist, "vector_search"))
            method = "vector_search"
        except Exception as exc:
            errors.append(f"vector_search: {type(exc).__name__}: {str(exc)[:200]}")
            hits = []

    if not hits and embed is not None and "local_cosine" not in disable:
        try:
            qv = embed(q)
            scored = [(cosine(qv, r.embedding), r) for r in load_runbooks() if r.embedding]
            if not scored:
                raise RuntimeError("no stored embeddings")
            scored.sort(key=lambda s: -s[0])
            hits = [RunbookHit(r.runbook_id, r.title, r.failure_signature, sim, "local_cosine")
                    for sim, r in scored[:top_k]]
            method = "local_cosine"
        except Exception as exc:
            errors.append(f"local_cosine: {type(exc).__name__}: {str(exc)[:200]}")
            hits = []

    if not hits:
        try:
            qt = tokens(q)
            scored = []
            for r in load_runbooks():
                doc = tokens(f"{r.failure_signature} {r.title}")
                overlap = len(qt & doc) / len(doc) if doc else 0.0
                scored.append((max(overlap, signature_match(r.failure_signature, incident.root_cause.alert_type)), r))
            scored.sort(key=lambda s: -s[0])
            hits = [RunbookHit(r.runbook_id, r.title, r.failure_signature, s, "keyword")
                    for s, r in scored[:top_k] if s > 0]
            method = "keyword"
        except Exception as exc:
            errors.append(f"keyword: {type(exc).__name__}: {str(exc)[:200]}")

    for e in errors:
        log.warning("retrieval degraded: %s", e)

    try:
        history = source.runbook_history()
    except Exception as exc:
        errors.append(f"history: {type(exc).__name__}")
        history = {}
    return RetrievalResult(q, method, rerank(hits, incident, history), errors)


def rerank(hits: list[RunbookHit], incident: Incident, history: dict[str, tuple[int, int]]) -> list[RunbookHit]:
    root_type = incident.root_cause.alert_type
    symptom_types = {s["alert_type"] for s in incident.signatures if s["alert_type"] != root_type}
    for h in hits:
        ok, total = history.get(h.runbook_id, (0, 0))
        sig = signature_match(h.failure_signature, root_type)
        symptom = max((signature_match(h.failure_signature, t) for t in symptom_types), default=0.0)
        h.components = {
            "similarity": round(h.similarity, 4),
            "signature": round(sig, 3),
            "history": round(ok / total, 3) if total else 0.5,
            # Only penalise a symptom match that beats the root-cause match.
            "symptom_penalty": round(symptom if symptom > sig else 0.0, 3),
        }
        h.score = round(sum(RERANK_WEIGHTS[k] * v for k, v in h.components.items()), 4)
    return sorted(hits, key=lambda h: -h.score)
