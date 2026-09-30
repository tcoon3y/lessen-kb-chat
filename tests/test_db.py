import json
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import agent, db, docs_requests, main, suggestions, usage

pgserver = pytest.importorskip("pgserver")


@pytest.fixture(scope="module")
def pg_uri():
    srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    yield srv.get_uri()


@pytest.fixture(autouse=True)
def env(monkeypatch, pg_uri):
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("AUTH_MODE", "passcode")
    monkeypatch.setenv("APP_PASSCODE", "letmein")
    monkeypatch.setenv("DOCS_REQUEST_PAGE_ID", "1")
    assert db.init()
    with db._connect() as c:
        c.execute("TRUNCATE chats, events, api_calls")
    usage.reset()
    suggestions.reset()


client = TestClient(main.app)
H = {"X-Passcode": "letmein"}


def done_event(text="Answer", sources=None, nd=False):
    return {"type": "done", "text": text, "sources": sources if sources is not None else [{"title": "P", "url": "u"}],
            "not_documented": nd, "meta": {"latency_ms": 1500, "tool_calls": ["search_confluence"],
                                           "tokens": {"input": 10000, "output": 500, "cache_read": 0, "cache_write": 0}}}


def test_chat_is_stored_with_text_and_cost(monkeypatch):
    monkeypatch.setattr(agent, "stream_answer", lambda q, h, **kw: iter([done_event("QBO syncs invoices.")]))
    client.post("/api/chat", headers=H, json={"question": "Does QBO sync invoices?", "mode": "product",
                                               "session_id": "tab-1"})
    with db._connect() as c:
        row = c.execute("SELECT mode, question, answer, outcome, session_id, input_tokens, cost_usd, sources "
                        "FROM chats").fetchone()
    assert row[:6] == ("product", "Does QBO sync invoices?", "QBO syncs invoices.", "answered", "tab-1", 10000)
    assert float(row[6]) == pytest.approx(0.0375) and row[7][0]["title"] == "P"


def test_errors_and_events_are_stored(monkeypatch):
    def boom(q, h, **kw):
        raise RuntimeError("x")
        yield
    monkeypatch.setattr(agent, "stream_answer", boom)
    client.post("/api/chat", headers=H, json={"question": "q"})
    monkeypatch.setattr(agent, "make_subject", lambda q: "Subj")
    monkeypatch.setattr(docs_requests, "add_request", lambda s, q, kind="Request": None)
    client.post("/api/request-docs", headers=H, json={"subject": "Tax", "question": "How is tax mapped?"})
    client.post("/api/feedback", headers=H, json={"question": "q2", "note": "wrong status", "mode": "cs",
                                                   "sources": ["Page A"]})
    with db._connect() as c:
        assert c.execute("SELECT outcome, error_kind FROM chats").fetchone() == ("error", "unknown")
        ev = c.execute("SELECT kind, subject, note FROM events ORDER BY id").fetchall()
    assert ev == [("doc_request", "Tax", None), ("incorrect_answer", "Subj", "wrong status")]


def test_usage_and_popular_come_from_db(monkeypatch):
    monkeypatch.setattr(agent, "stream_answer", lambda q, h, **kw: iter([done_event()]))
    for _ in range(2):
        client.post("/api/chat", headers=H, json={"question": "How does autopay work?", "mode": "cs"})
    usage.reset()  # simulate a redeploy: in-memory counts are gone, the database still has them
    suggestions.reset()
    u = client.get("/api/usage", headers=H).json()
    assert u["stored"] is True and u["claude"]["questions_month"] == 2 and u["claude"]["cost_month"] > 0
    assert client.get("/api/suggestions?mode=cs", headers=H).json()["questions"][0] == "How does autopay work?"


def test_api_calls_counted_by_service():
    usage.record_confluence(200, {}, "atlassian")
    usage.record_confluence(429, {}, "notion")
    with db._connect() as c:
        rows = dict(c.execute("SELECT service, calls FROM api_calls").fetchall())
    assert rows == {"atlassian": 1, "notion": 1}


def test_retention_clears_text_but_keeps_counts(monkeypatch):
    db.log_chat("cs", "old question", "old answer", [], "answered", {"tokens": {"input": 5}})
    with db._connect() as c:
        c.execute("UPDATE chats SET created_at = now() - interval '400 days'")
    db.cleanup()
    with db._connect() as c:
        assert c.execute("SELECT question, answer, input_tokens FROM chats").fetchone() == (None, None, 5)


def test_bot_keeps_working_when_db_is_down(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    assert db.init() is False
    monkeypatch.setattr(agent, "stream_answer", lambda q, h, **kw: iter([done_event("still works")]))
    evs = [json.loads(c[6:]) for c in client.post("/api/chat", headers=H, json={"question": "q"}).text.split("\n\n")
           if c.startswith("data: ")]
    assert evs[-1]["text"] == "still works"
