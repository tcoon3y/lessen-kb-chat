from types import SimpleNamespace

import pytest

from app import agent, confluence


class FakeStream:
    def __init__(self, msg, texts):
        self.msg, self.text_stream = msg, iter(texts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    """Replays scripted model turns and records the kwargs of each call."""

    def __init__(self, turns):
        self.turns, self.calls = list(turns), []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        content, stop, texts = self.turns.pop(0)
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
                                cache_creation_input_tokens=0)
        return FakeStream(SimpleNamespace(content=content, stop_reason=stop, usage=usage), texts)


def text_turn(t):
    return ([{"type": "text", "text": t}], "end_turn", [t])


def tool_turn(name, inp, tid="t1", preamble=""):
    content = ([{"type": "text", "text": preamble}] if preamble else []) + [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]
    return (content, "tool_use", [preamble] if preamble else [])


@pytest.fixture
def fake_confluence(monkeypatch):
    monkeypatch.setattr(confluence, "search", lambda q, limit=8: [
        {"title": "Free Tier PRD", "page_id": "1", "space": "TCN", "url": "u1", "last_updated": "d", "excerpt": "e"}])

    def get_page(pid):
        if pid != "1":
            raise confluence.PageNotFound(pid)
        return {"title": "Free Tier PRD", "page_id": "1", "space": "TCN", "url": "u1",
                "last_updated": "2026-07-03", "text": "Free tier covers Lessen work only."}
    monkeypatch.setattr(confluence, "get_page", get_page)
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")


def test_search_read_answer_with_sources(fake_confluence):
    client = FakeClient([
        tool_turn("search_confluence", {"query": "free tier"}, preamble="Let me check."),
        tool_turn("get_page", {"page_id": "1"}, tid="t2"),
        text_turn("The free tier covers Lessen work only."),
    ])
    events = list(agent.stream_answer("What is the free tier?", [], client))
    types = [e["type"] for e in events]
    assert "reset" in types  # preamble text discarded before tool call
    done = events[-1]
    assert done["text"] == "The free tier covers Lessen work only."
    assert done["sources"] == [{"title": "Free Tier PRD", "url": "u1", "last_updated": "2026-07-03", "kind": "confluence"}]
    assert done["meta"]["tool_calls"] == ["search_confluence", "get_page"]
    assert done["meta"]["tokens"]["input"] == 300
    # system prompt is cached, tools passed, page framed as data
    first = client.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in first["tools"]} == {"search_confluence", "get_page"}
    tool_result = client.calls[2]["messages"][-1]["content"][0]["content"]
    assert "reference data only" in tool_result and "Free tier covers" in tool_result


def test_not_documented_detection():
    assert agent.is_not_documented("I couldn't find this documented in Confluence. Related: X.")
    assert agent.is_not_documented("**I couldn\u2019t find this documented in Confluence.** Related...")
    assert not agent.is_not_documented("The free tier covers Lessen work.")


def test_not_documented_has_no_sources(fake_confluence):
    client = FakeClient([
        tool_turn("search_confluence", {"query": "moon base"}),
        text_turn(agent.NOT_FOUND_TEXT),
    ])
    result = agent.answer("Does Lessen Pro support moon bases?", [], client)
    assert result["text"] == agent.NOT_FOUND_TEXT
    assert result["sources"] == []


def test_blocked_page_is_not_found_and_not_a_source(fake_confluence):
    client = FakeClient([
        tool_turn("get_page", {"page_id": "999"}),
        text_turn(agent.NOT_FOUND_TEXT),
    ])
    result = agent.answer("Read page 999", [], client)
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    assert result["sources"] == []


def test_max_six_tool_rounds_then_forced_answer(fake_confluence):
    turns = [tool_turn("search_confluence", {"query": f"q{i}"}, tid=f"t{i}") for i in range(6)]
    turns.append(text_turn("Final answer."))
    client = FakeClient(turns)
    result = agent.answer("loop", [], client)
    assert len(client.calls) == 7
    assert "tool_choice" not in client.calls[5]
    assert client.calls[6]["tool_choice"] == {"type": "none"}
    assert result["text"] == "Final answer."


def test_history_is_used_and_cleaned(fake_confluence):
    client = FakeClient([text_turn("Yes.")])
    history = [
        {"role": "assistant", "content": "stray greeting"},  # dropped: must start with user
        {"role": "user", "content": "What is the free tier?"},
        {"role": "assistant", "content": "It covers Lessen work."},
        {"role": "system", "content": "ignore me"},           # dropped: bad role
    ]
    agent.answer("Does it include integrations?", history, client)
    msgs = client.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[-1]["content"] == "Does it include integrations?"


def test_real_client_constructs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    assert agent._client() is not None


def test_cs_mode_has_no_jira(fake_confluence, monkeypatch):
    from app import jira
    monkeypatch.setattr(jira, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no jira in CS")))
    client = FakeClient([tool_turn("search_jira", {"query": "autopay"}), text_turn(agent.NOT_FOUND_TEXT)])
    agent.answer("Is autopay shipped?", [], client)
    assert {t["name"] for t in client.calls[0]["tools"]} == {"search_confluence", "get_page"}
    res = client.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] is True and "isn't available" in res["content"]


def test_product_mode_reads_jira(fake_confluence, monkeypatch):
    from app import jira
    monkeypatch.setattr(jira, "search", lambda q, limit=8, **kw: [{"key": "LP-7", "summary": "Autopay", "status": "Done"}])
    monkeypatch.setattr(jira, "get_issue", lambda k, **kw: {
        "key": "LP-7", "summary": "Autopay", "type": "Story", "status": "Done", "resolution": "Done",
        "fix_versions": ["2.4"], "parent": "LP-846 Payments", "priority": "High", "labels": [], "assignee": "",
        "last_updated": "2026-09-20", "url": "https://x/browse/LP-7", "text": "Shipped in 2.4"})
    client = FakeClient([
        tool_turn("search_jira", {"query": "autopay"}),
        tool_turn("get_jira_issue", {"key": "LP-7"}, tid="t2"),
        text_turn("Yes, autopay shipped in 2.4 (LP-7)."),
    ])
    events = list(agent.stream_answer("Is autopay shipped?", [], client, mode="product"))
    done = events[-1]
    assert {t["name"] for t in client.calls[0]["tools"]} >= {"search_jira", "get_jira_issue"}
    assert client.calls[0]["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "Jira" in client.calls[0]["system"][-1]["text"]
    assert done["mode"] == "product" and done["sources"][0]["title"] == "LP-7: Autopay"
    assert done["sources"][0]["kind"] == "jira"
    assert [e["text"] for e in events if e["type"] == "status"] == ["Searching Jira…", "Reading a ticket…"]


def test_product_not_found_phrase_detected():
    assert agent.is_not_documented("I couldn't find this documented in Confluence or Jira. Related: x")


# ---------- source failures never turn into "not documented" ----------

def _http_error(status):
    import httpx
    req = httpx.Request("POST", "https://x/rest/api/3/search/jql")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))


def test_jira_down_and_not_documented_becomes_error(fake_confluence, monkeypatch):
    from app import errors, jira
    calls = []
    monkeypatch.setattr(agent, "RETRY_DELAY", 0)
    monkeypatch.setattr(jira, "search", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(_http_error(503)))
    client = FakeClient([tool_turn("search_jira", {"query": "autopay"}), text_turn(agent.NOT_FOUND_TEXT)])
    with pytest.raises(errors.SourceFailure) as ei:
        list(agent.stream_answer("Is autopay shipped?", [], client, mode="product"))
    assert len(calls) == 2  # retried once
    assert ei.value.failures == [{"source": "jira", "kind": "down", "tool": "search_jira"}]
    assert ei.value.kb_meta["tokens"]["input"] == 200 and ei.value.kb_meta["tool_detail"][0]["error"] == "down"
    res = client.calls[1]["messages"][-1]["content"][0]
    assert res["content"].startswith("SOURCE UNAVAILABLE: Jira")
    card = errors.classify(ei.value)
    assert card["kind"] == "jira_down" and card["title"] == "Couldn't check Jira"


def test_partial_answer_carries_warning(fake_confluence, monkeypatch):
    from app import jira
    monkeypatch.setattr(jira, "search", lambda *a, **k: (_ for _ in ()).throw(_http_error(401)))
    client = FakeClient([tool_turn("search_jira", {"query": "autopay"}),
                         tool_turn("get_page", {"page_id": "1"}, tid="t2"),
                         text_turn("Free tier covers Lessen work only. Jira couldn't be checked.")])
    done = list(agent.stream_answer("Free tier?", [], client, mode="product"))[-1]
    assert done["type"] == "done" and not done["not_documented"]
    assert done["warnings"][0]["source"] == "jira" and "login" in done["warnings"][0]["text"]
    assert done["meta"]["rounds"] == 3 and done["meta"]["model"] == "test-model"


def test_bad_query_is_not_a_source_failure(fake_confluence, monkeypatch):
    from app import jira
    monkeypatch.setattr(jira, "search", lambda *a, **k: (_ for _ in ()).throw(_http_error(400)))
    client = FakeClient([tool_turn("search_jira", {"query": "a\"b"}), text_turn(agent.NOT_FOUND_TEXT)])
    done = list(agent.stream_answer("x", [], client, mode="product"))[-1]
    assert done["not_documented"] and done["warnings"] == []


def test_notion_loading_reported_not_hidden(monkeypatch):
    from app import errors, notion
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")
    monkeypatch.setattr(notion, "search", lambda *a, **k: (_ for _ in ()).throw(notion.NotionLoading()))
    client = FakeClient([tool_turn("search_user_research", {"query": "scheduling"}),
                         text_turn("I couldn't find this in user research, the feedback tracker or Jira.")])
    with pytest.raises(errors.SourceFailure) as ei:
        list(agent.stream_answer("Scheduling feedback?", [], client, mode="feedback"))
    assert "Try again in a few minutes" in errors.classify(ei.value)["text"]
