"""SRE console backend: AG-UI agent endpoint + REST API + the built console.

    uvicorn agent.app.server:app --port 8080

POST /agent                       AG-UI stream of the ADK graph (reasoning, tool calls, state)
GET  /api/...                     reads for the console (all responses redacted)
POST /api/approvals/{id}/approve  human-only actions; the approver is the IAP identity when
POST /api/approvals/{id}/execute  deployed behind IAP, otherwise a required `by` field
POST /api/incidents/{id}/resolve
POST /api/postmortems/{id}/publish
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_settings
from .logging_setup import setup_logging
from .redact import redact_obj
from .service import ServiceError, SREService
from .tools.approvals import ApprovalError

setup_logging()
_settings = load_settings()
# ADK talks to Gemini through Vertex AI in this project.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _settings.project)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", os.environ.get("GEMINI_LOCATION", "global"))

app = FastAPI(title="SRE Incident Console", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o],
    allow_methods=["*"], allow_headers=["*"],
)
_svc: SREService | None = None


def svc() -> SREService:
    global _svc
    if _svc is None:
        _svc = SREService(_settings)
    return _svc


def ok(data: Any) -> JSONResponse:
    return JSONResponse(redact_obj(json.loads(json.dumps(data, default=str))))


def actor(request: Request, body_by: str | None) -> str:
    """IAP identity when present, else the name typed in the console."""
    iap = request.headers.get("x-goog-authenticated-user-email", "")
    who = iap.split(":", 1)[-1] if iap else (body_by or "").strip()
    if not who:
        raise HTTPException(400, "a named human is required for this action")
    return who


@app.exception_handler(ServiceError)
@app.exception_handler(ApprovalError)
async def refused(_: Request, exc: Exception):
    return JSONResponse({"error": str(exc)}, status_code=409)


class Who(BaseModel):
    by: str | None = None
    reason: str = ""


class PostmortemReq(BaseModel):
    publish: bool = False


# ------------------------------------------------------------------ reads
@app.get("/api/health")
def health():
    return {"ok": True, "project": _settings.project, "location": _settings.location}


@app.get("/api/metrics")
def metrics():
    return ok(svc().metrics())


@app.get("/api/incidents")
def incidents(limit: int = 50):
    return ok(svc().list_incidents(limit))


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str):
    return ok(svc().incident_detail(incident_id))


@app.get("/api/incidents/{incident_id}/timeline")
def timeline(incident_id: str, bucket: int = 10):
    return ok(svc().timeline(incident_id, bucket))


@app.get("/api/incidents/{incident_id}/impact")
def impact(incident_id: str):
    return ok(svc().impact(incident_id).to_dict())


@app.get("/api/topology")
def topology(incident_id: str | None = None):
    return ok(svc().topology(incident_id))


@app.get("/api/forecasts")
def forecasts(horizon: float = 60.0, explain: bool = False):
    return ok([asdict(f) for f in svc().forecasts(horizon=horizon, explain=explain)])


@app.get("/api/approvals/pending")
def pending():
    from .tools.approvals import Approvals
    return ok(Approvals(svc().wh).pending())


@app.post("/api/triage")
def scan(persist: bool = False):
    res = svc().triage()
    out = res.to_dict()
    if persist and res.incidents:
        incident_id, created = svc().open_incident(res.incidents[0])
        out["opened"] = {"incident_id": incident_id, "created": created}
    return ok(out)


# ------------------------------------------------------------------ human-only actions
@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Who, request: Request):
    who = actor(request, body.by)
    svc().decide(approval_id, True, who, body.reason)
    return ok({"approval_id": approval_id, "status": "APPROVED", "by": who})


@app.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: str, body: Who, request: Request):
    who = actor(request, body.by)
    svc().decide(approval_id, False, who, body.reason)
    return ok({"approval_id": approval_id, "status": "REJECTED", "by": who})


@app.post("/api/approvals/{approval_id}/execute")
def execute(approval_id: str, body: Who, request: Request):
    actor(request, body.by)  # executing is a human action too; the gate re-checks the approval row
    res = svc().execute(approval_id)
    return ok(asdict(res))


@app.post("/api/incidents/{incident_id}/resolve")
def resolve(incident_id: str, body: Who, request: Request):
    who = actor(request, body.by)
    return ok({"incident_id": incident_id, "resolved_at": svc().resolve(incident_id, who), "by": who})


@app.post("/api/incidents/{incident_id}/postmortem")
def postmortem(incident_id: str, body: PostmortemReq):
    d, published = svc().write_postmortem(incident_id, publish=body.publish)
    return ok({**asdict(d), "published": published})


@app.post("/api/postmortems/{postmortem_id}/publish")
def publish(postmortem_id: str, body: Who, request: Request):
    who = actor(request, body.by)
    svc().publish_postmortem(postmortem_id)
    return ok({"postmortem_id": postmortem_id, "published": True, "by": who})


# ------------------------------------------------------------------ AG-UI agent
def _mount_agent() -> None:
    from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint
    from .agents import get_root_agent

    agent = ADKAgent(adk_agent=get_root_agent(), app_name="sre_console", user_id="console",
                     use_in_memory_services=True, execution_timeout_seconds=900, tool_timeout_seconds=600)
    add_adk_fastapi_endpoint(app, agent, path="/agent")


if os.environ.get("DISABLE_AGENT") != "1":
    _mount_agent()


# ------------------------------------------------------------------ console (built SPA)
DIST = Path(__file__).resolve().parents[2] / "console" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
