import json

import pytest
from fastapi.testclient import TestClient

from app import agent, docs_requests, main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "passcode")
    monkeypatch.setenv("APP_PASSCODE", "letmein")
    monkeypatch.setenv("DOCS_REQUEST_PAGE_ID", "3972071438")


def fake_stream(question, history):
    yield {"type": "status", "text": "Searching Confluence…"}
    yield {"type": "text", "text": "Hello "}
    yield {"type": "text", "text": f"({len(history)} prior turns)"}
    yield {"type": "done", "text": "Hello", "sources": [], "not_documented": False, "meta": {}}


def events(resp):
    return [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]


def test_config_and_login():
    assert client.get("/api/config").json() == {"passcode_required": True, "docs_requests_enabled": True}
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
    def boom(q, h):
        raise RuntimeError("secret detail")
        yield
    monkeypatch.setattr(agent, "stream_answer", boom)
    evs = events(client.post("/api/chat", headers={"X-Passcode": "letmein"}, json={"question": "x"}))
    assert evs == [{"type": "error", "text": main.FRIENDLY_ERROR}]


def test_cloudflare_mode_skips_passcode(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "cloudflare")
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    assert client.post("/api/chat", json={"question": "hi"}).status_code == 200


def test_request_docs(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "make_subject", lambda q: "Crypto payouts")
    monkeypatch.setattr(docs_requests, "add_request", lambda s, q: calls.append((s, q)))
    r = client.post("/api/request-docs", headers={"X-Passcode": "letmein"}, json={"question": "Crypto?"})
    assert r.json() == {"ok": True} and calls == [("Crypto payouts", "Crypto?")]
    assert client.post("/api/request-docs", json={"question": "x"}).status_code == 401


def test_page_served_and_no_api_docs():
    assert "Lessen Pro KB Chat" in client.get("/").text
    assert client.get("/docs").status_code == 404
