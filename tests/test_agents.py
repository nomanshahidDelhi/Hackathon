"""Run the real ADK graph end to end with a scripted model and the real tools on
simulated kit data: proves routing, tool calls and state hand-offs between agents."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

from agent.app import agents
from agent.app.tools.clustering import triage
from agent.app.tools.impact import summarize
from agent.app.tools.retrieval import retrieve
from tests.fixtures import kit_sim
from tests.fixtures.kit_runbooks import load_runbooks
from tests.test_remediation import FakeSource

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class ScriptedModel(BaseLlm):
    """Calls the agent's first tool (or transfers), then summarises the tool result."""
    model: str = "scripted"
    calls: list = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        last = llm_request.contents[-1].parts[-1] if llm_request.contents else None
        tools = [t for t in (llm_request.tools_dict or {}) if t != "transfer_to_agent"]
        already = {p.function_response.name for c in llm_request.contents for p in c.parts or []
                   if p.function_response is not None}
        todo = [t for t in tools if t not in already]
        if last is not None and last.function_response is not None and not todo:
            text = f"done: {last.function_response.name}"
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
            return
        if todo:
            name = todo[0]
            args = {"incident_id": "inc-5019"} if name == "draft_incident_postmortem" else {}
            ScriptedModel.calls.append(name)
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(name=name, args=args))]))
            return
        target = "postmortem_agent" if "postmortem" in str(llm_request.contents[0]).lower() else "incident_response"
        ScriptedModel.calls.append(f"transfer:{target}")
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="transfer_to_agent", args={"agent_name": target}))]))


class FakeService:
    def __init__(self):
        self.nodes, self.alerts = kit_sim.load_nodes(), kit_sim.generate(T0)
        self.runbooks = {r.runbook_id: r for r in load_runbooks()}
        self.approvals, self.opened = [], []

    def triage(self, lookback=None):
        start = T0 - timedelta(minutes=60)
        base = Counter((a.node_id, a.alert_type) for a in self.alerts if start - timedelta(days=7) <= a.timestamp < start)
        return triage(self.alerts, self.nodes, base, 7 * 1440.0, start, T0)

    def open_incident(self, inc):
        self.opened.append(inc)
        return "inc-5019", True

    def find_runbook(self, inc):
        return retrieve(inc, FakeSource(self.runbooks, order=["sop-106", "sop-102", "sop-101"]))

    def propose(self, inc):
        from agent.app.tools.remediation import propose
        from executor.sandbox import Sandbox
        r = self.find_runbook(inc)
        return r, propose(inc, r, self.runbooks, Sandbox(), None)

    def request_approval(self, incident_id, proposal):
        self.approvals.append((incident_id, proposal.runbook_id))
        return "apr-test"

    def forecasts(self, horizon=60.0):
        from agent.app.tools.forecast import compute_forecasts
        win = [a for a in self.alerts if T0 - timedelta(minutes=120) <= a.timestamp <= T0]
        return compute_forecasts(win, self.nodes, T0)

    def impact(self, incident_id):
        return summarize(incident_id, [])


def run(prompt: str):
    from google.adk.runners import InMemoryRunner

    async def go():
        runner = InMemoryRunner(agent=agents.build_agents(), app_name="t")
        s = await runner.session_service.create_session(app_name="t", user_id="u")
        async for _ in runner.run_async(user_id="u", session_id=s.id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
            pass
        return await runner.session_service.get_session(app_name="t", user_id="u", session_id=s.id)
    return asyncio.run(go())


@pytest.fixture
def wired(monkeypatch):
    pytest.importorskip("executor.sandbox").find_bash()
    fake = FakeService()
    monkeypatch.setattr(agents, "_service", fake)
    monkeypatch.setattr(agents, "MODEL", ScriptedModel())
    ScriptedModel.calls = []
    return fake


def test_incident_response_pipeline_runs_every_agent(wired):
    session = run("Alerts are exploding in ca-central-1, what is happening?")
    calls = ScriptedModel.calls
    assert calls[0] == "transfer:incident_response"
    for tool in ("triage_alert_storm", "open_incident", "find_runbook", "propose_and_request_approval",
                 "forecast_breaches", "business_impact"):
        assert tool in calls, calls
    st = session.state
    assert st["incident"]["root_cause"]["alert_type"] == "connection_pool_exhausted"
    assert st["incident_id"] == "inc-5019"
    assert st["runbook_id"] == "sop-102"          # despite vector order putting the 5xx SOP first
    assert st["approval_id"] == "apr-test"
    assert wired.approvals == [("inc-5019", "sop-102")]
    assert st["forecast_count"] == 6
    for key in ("triage_summary", "diagnosis_summary", "remediation_summary", "forecast_summary", "impact_summary"):
        assert st.get(key), key


def test_agents_have_no_way_to_approve_or_execute():
    root = agents.build_agents()
    names = set()

    def walk(a):
        names.update(getattr(t, "__name__", "") for t in getattr(a, "tools", []))
        for s in a.sub_agents:
            walk(s)
    walk(root)
    assert names and not any(w in n for n in names for w in ("approve", "execute", "resolve", "publish", "decide"))


def test_downstream_steps_open_the_incident_even_if_the_llm_skips_it(wired, monkeypatch):
    # A model that never calls open_incident: approval and impact must still get an incident id.
    orig = ScriptedModel.generate_content_async

    async def skip_open(self, llm_request, stream=False):
        if "open_incident" in (llm_request.tools_dict or {}):
            llm_request.tools_dict = {k: v for k, v in llm_request.tools_dict.items() if k != "open_incident"}
        async for r in orig(self, llm_request, stream):
            yield r
    monkeypatch.setattr(ScriptedModel, "generate_content_async", skip_open)
    session = run("what is happening?")
    assert "open_incident" not in ScriptedModel.calls
    assert session.state["approval_id"] == "apr-test" and session.state["incident_id"] == "inc-5019"
