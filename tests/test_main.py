import json

import pytest
from fastapi.testclient import TestClient

from app import agent, docs_requests, errors, main, suggestions, usage

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "passcode")
    monkeypatch.setenv("APP_PASSCODE", "letmein")
    monkeypatch.setenv("DOCS_REQUEST_PAGE_ID", "3972071438")
    usage.reset()


def fake_stream(question, history, **kw):
    yield {"type": "status", "text": "Searching Confluence…"}
    yield {"type": "text", "text": "Hello "}
    yield {"type": "text", "text": f"({len(history)} prior turns)"}
    yield {"type": "done", "text": "Hello", "sources": [], "not_documented": False, "meta": {}}


def events(resp):
    return [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]


def test_config_and_login():
    cfg = client.get("/api/config").json()
    assert cfg["passcode_required"] is True and cfg["docs_requests_enabled"] is True
    assert cfg["docs_requests_url"].endswith("pageId=3972071438")
    assert client.post("/api/login", headers={"X-Passcode": "nope"}).status_code == 401
    assert client.post("/api/login", headers={"X-Passcode": "letmein"}).json() == {"ok": True}


def test_chat_requires_passcode(monkeypatch):
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    assert client.post("/api/chat", json={"question": "hi"}).status_code == 401


def test_chat_streams_sse_with_history(monkeypatch):
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    r = client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={
        "question": "follow up", "history": [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = events(r)
    assert [e["type"] for e in evs] == ["status", "text", "text", "done"]
    assert evs[2]["text"] == "(2 prior turns)"


def test_chat_error_is_friendly(monkeypatch):
    def boom(q, h, **kw):
        raise RuntimeError("secret detail")
        yield
    monkeypatch.setattr(agent, "stream_answer", boom)
    evs = events(client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={"question": "x"}))
    assert len(evs) == 1 and evs[0]["type"] == "error" and evs[0]["kind"] == "unknown"
    assert "secret detail" not in json.dumps(evs)


def test_cloudflare_mode_skips_passcode(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "cloudflare")
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    assert client.post("/api/chat", json={"question": "hi"}).status_code == 200


def test_request_docs(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "make_subject", lambda q: "Crypto payouts")
    monkeypatch.setattr(docs_requests, "add_request", lambda s, q, kind="Request": calls.append((s, q, kind)))
    r = client.post("/api/request-docs", headers={"X-Passcode": "letmein"}, json={"question": "Crypto?"})
    assert r.json() == {"ok": True} and calls == [("Crypto payouts", "Crypto?", "Not documented")]
    assert client.post("/api/request-docs", json={"question": "x"}).status_code == 401


def test_page_served_and_no_api_docs():
    assert "Lessen Pro Knowledge Bot" in client.get("/").text
    assert client.get("/docs").status_code == 404


def test_confluence_login_failure_is_clear(monkeypatch):
    def no_login(q, h, **kw):
        raise errors.ConfluenceLoginError()
        yield
    monkeypatch.setattr(agent, "stream_answer", no_login)
    evs = events(client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={"question": "x"}))
    assert evs[0]["type"] == "error" and evs[0]["kind"] == "confluence_login"


def test_feedback_adds_row(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "make_subject", lambda q: "QBO sync")
    monkeypatch.setattr(docs_requests, "add_request", lambda s, q, kind="Request": calls.append((s, q, kind)))
    r = client.post("/api/feedback", headers={"X-Passcode": "letmein"},
                    json={"question": "Does QBO sync?", "note": "Tax sync is wrong", "sources": ["QBO Getting Started"]})
    assert r.json() == {"ok": True}
    assert calls == [("QBO sync", "Does QBO sync? | Feedback: Tax sync is wrong | Cited: QBO Getting Started",
                      "Incorrect answer")]
    assert client.post("/api/feedback", json={"question": "x"}).status_code == 401


def test_popular_suggestions(monkeypatch):
    suggestions.reset()
    monkeypatch.delenv("SUGGESTED_QUESTIONS", raising=False)

    def cited(q, h, **kw):
        yield {"type": "done", "text": "x", "sources": [{"title": "t", "url": "u"}], "not_documented": False, "meta": {}}
    monkeypatch.setattr(agent, "stream_answer", cited)
    for q in ["How do I connect QBO?", "how do i connect qbo", "A one-off personal question?"]:
        client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={"question": q})
    got = client.get("/api/suggestions", headers={"X-Passcode": "letmein"}).json()["questions"]
    assert got[0].lower().startswith("how do i connect qbo") and len(got) == 3
    assert "A one-off personal question?" not in got   # asked only once
    assert client.get("/api/suggestions").status_code == 401


def test_request_docs_with_subject_skips_claude(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "make_subject", lambda q: (_ for _ in ()).throw(AssertionError("no Claude call")))
    monkeypatch.setattr(docs_requests, "add_request", lambda s, q, kind="Request": calls.append((s, q, kind)))
    r = client.post("/api/request-docs", headers={"X-Passcode": "letmein"},
                    json={"subject": "QBO tax sync", "question": "How are tax rates mapped?"})
    assert r.json() == {"ok": True} and calls == [("QBO tax sync", "How are tax rates mapped?", "Request")]


def test_rate_limit_and_usage_without_text(monkeypatch, caplog):
    def ok(q, h, **kw):
        yield {"type": "done", "text": "SECRET ANSWER", "sources": [], "not_documented": False,
               "meta": {"latency_ms": 2000, "tool_calls": ["search_confluence"],
                        "tokens": {"input": 1000, "output": 100, "cache_read": 0, "cache_write": 0}}}
    monkeypatch.setattr(agent, "stream_answer", ok)
    caplog.set_level("INFO", logger="kbchat")
    for i in range(usage.RATE_LIMIT):
        evs = events(client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={"question": "SECRET QUESTION"}))
        assert evs[-1]["type"] == "done"
    blocked = events(client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={"question": "one more"}))
    assert blocked[0]["kind"] == "rate_limited"
    u = client.get("/api/usage", headers={"X-Passcode": "letmein"}).json()
    assert u["claude"]["cost_month"] > 0 and len(u["claude"]["series"]) == 14
    assert u["claude"]["questions_month"] == usage.RATE_LIMIT + 1 and u["error_kinds"] == {"rate_limited": 1}
    assert "SECRET" not in caplog.text and '"latency_ms": 2000' in caplog.text
    assert client.get("/api/usage").status_code == 401
