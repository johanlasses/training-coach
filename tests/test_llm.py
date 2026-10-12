import json
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app import coach, llm
from app.config import Config
from app.db import DB

from .test_coach import DATES, good_plan


def sse(text: str, stop_reason: str = "end_turn") -> str:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": llm.MODEL, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


def mock_api(monkeypatch, body: str) -> list:
    """The SDK talks httpx2, so inject a mock transport rather than patching httpx."""
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    real = anthropic.AsyncAnthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: real(
        **kw, http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))))
    return requests


async def test_run_sends_schema_fallbacks_and_effort(monkeypatch):
    requests = mock_api(monkeypatch, sse('{"ok": true}'))
    msg = await llm.run("key", "sys", [{"role": "user", "content": "hi"}], schema={"type": "object"}, effort="high")
    assert llm.text_of(msg) == '{"ok": true}'
    req = requests[0]
    body = json.loads(req.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]


async def test_run_raises_on_refusal(monkeypatch):
    mock_api(monkeypatch, sse("", "refusal"))
    with pytest.raises(llm.LLMError, match="declined"):
        await llm.run("key", "sys", [{"role": "user", "content": "hi"}])


async def test_run_requires_key():
    with pytest.raises(llm.LLMError, match="No Anthropic API key"):
        await llm.run("", "sys", [])


async def test_generate_plan_retries_with_errors(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    bad = good_plan()
    bad["days"][5]["duration_min"] = 400
    replies = [json.dumps(bad), json.dumps(good_plan())]
    calls = []

    async def fake_run(api_key, system, messages, **kw):
        calls.append([dict(m) for m in messages])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=replies[len(calls) - 1])])

    monkeypatch.setattr(llm, "run", fake_run)
    snapshot = {"recent_rides": [], "load": {"ctl": 50}}
    cfg = Config(anthropic_api_key="k")
    plan_id = await coach.generate_plan(db, cfg, snapshot, date(2026, 10, 12), date(2026, 10, 11))

    assert len(calls) == 2
    assert "400 min exceeds availability" in calls[1][-1]["content"]
    assert calls[1][-2]["role"] == "assistant"
    stored = db.plan(plan_id)
    assert stored["warnings"] == []
    assert [d["date"] for d in stored["payload"]["days"]] == DATES
    assert stored["payload"]["block_week"] == 1


async def test_plan_prompt_uses_chosen_coach_and_athlete_goals(tmp_path, monkeypatch):
    from app import stats
    requests = mock_api(monkeypatch, sse(json.dumps(good_plan())))
    cfg = Config(anthropic_api_key="k", goals="Ride Gravel Unbound 200 in June", athlete_level="advanced")
    snapshot = stats.snapshot([], date(2026, 10, 11), cfg)
    plan_id = await coach.generate_plan(DB(tmp_path / "t.db"), cfg, snapshot, date(2026, 10, 12),
                                        date(2026, 10, 11), "carmichael")
    body = json.loads(requests[0].content)
    system = body["system"] if isinstance(body["system"], str) else body["system"][0]["text"]
    assert "time-crunched" in system
    assert "Ride Gravel Unbound 200 in June" in body["messages"][0]["content"]
    assert '"self_rated_level": "advanced"' in body["messages"][0]["content"]
    assert DB(tmp_path / "t.db").plan(plan_id)["coach"] == "carmichael"
