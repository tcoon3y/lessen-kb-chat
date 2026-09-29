import json

import httpx
import pytest

from app import agent, feedback_sheet, jira, notion
from tests.test_agent import FakeClient, text_turn, tool_turn

ROOT = "328878319aeb80f1839ffb8a08f4a609"
CHILD = "314878319aeb80c2a5ffeb14f704178e"
OUTSIDE = "99999999999999999999999999999999"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret_x")
    monkeypatch.setenv("NOTION_ROOT_PAGE_ID", ROOT)
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://lessen.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_EMAIL", "e")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "t")
    notion._state.update(pages={}, at=0.0, error=None, running=False)
    feedback_sheet._cache.update(at=0.0, data=None)


def para(text, has_children=False, bid="b1"):
    return {"id": bid, "type": "paragraph", "has_children": has_children,
            "paragraph": {"rich_text": [{"plain_text": text}]}}


def notion_handler(req):
    path = req.url.path
    assert req.method in ("GET", "POST")
    if path == f"/v1/pages/{ROOT}":
        return httpx.Response(200, json={"properties": {"t": {"type": "title", "title": [{"plain_text": "User Research"}]}},
                                         "last_edited_time": "2026-09-29T00:00:00Z"})
    if path == f"/v1/blocks/{ROOT}/children":
        return httpx.Response(200, json={"results": [
            para("Overview of research"),
            {"id": CHILD, "type": "child_page", "has_children": True, "last_edited_time": "2026-09-17T00:00:00Z",
             "child_page": {"title": "Scheduling & Optimization"}}], "has_more": False})
    if path == f"/v1/blocks/{CHILD}/children":
        return httpx.Response(200, json={"results": [
            {"id": "h", "type": "heading_2", "has_children": False, "heading_2": {"rich_text": [{"plain_text": "Findings"}]}},
            para("Pros found the drag and drop scheduler confusing on mobile", True, "nested")], "has_more": False})
    if path == "/v1/blocks/nested/children":
        return httpx.Response(200, json={"results": [para("Three of five testers missed the time picker")], "has_more": False})
    return httpx.Response(404)


def test_notion_crawl_search_and_limits():
    notion.refresh(client=httpx.Client(transport=httpx.MockTransport(notion_handler)))
    assert notion.status()["pages"] == 2 and notion.status()["error"] is None
    hits = notion.search("scheduler time picker")
    assert hits[0]["title"] == "Scheduling & Optimization" and hits[0]["path"] == "User Research"
    page = notion.get_page(CHILD)
    assert "## Findings" in page["text"] and "Three of five testers" in page["text"]
    with pytest.raises(notion.NotionPageNotFound):
        notion.get_page(OUTSIDE)  # never read pages outside the research tree


def test_notion_not_shared_is_an_error():
    notion.refresh(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    assert "404" in notion.status()["error"]
    with pytest.raises(PermissionError):
        notion.search("anything")


SHEET_PAGE = ("<p>intro</p><h2>Feedback Tracker</h2><table><tbody>"
              "<tr><th><p>Issue #</p></th><th><p>Short Description</p></th><th><p>Priority</p></th><th><p>Status</p></th></tr>"
              "<tr><td><p>15</p></td><td><p>Emails on Leads are delayed</p></td><td><p>p2</p></td><td><p>Email Logs</p></td></tr>"
              "<tr><td><p>16</p></td><td><p>Add technician to invoice filter</p></td><td><p>p2</p></td><td><p>Pending Dev</p></td></tr>"
              "</tbody></table><h2>Postive Feedback</h2><table><tbody><tr><th><p>User</p></th><th><p>Quote</p></th></tr>"
              "<tr><td><p>Elie</p></td><td><p>More straight forward than Lessen ONE</p></td></tr></tbody></table>")


def sheet_client():
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "body": {"storage": {"value": SHEET_PAGE}}, "version": {"when": "2026-09-29T11:55:00Z"}})))


def test_feedback_sheet_search_and_overview():
    res = feedback_sheet.search("invoice technician", client=sheet_client())
    assert res["matches"][0]["Issue #"] == "16" and res["matches"][0]["sheet"] == "Feedback Tracker"
    ov = feedback_sheet.search("", client=sheet_client())["overview"]
    assert ov[0]["rows"] == 2 and ov[0]["by_priority"] == {"P2": 2} and ov[1]["sheet"] == "Postive Feedback"


def test_feedback_mode_tools_and_jira_scope(monkeypatch):
    monkeypatch.setattr(feedback_sheet, "search", lambda q="", sheet="", limit=25: {
        "matches": [{"sheet": "Feedback Tracker", "Short Description": "Emails delayed"}], "synced": "d",
        "source_url": "https://x/xlsx"})
    seen = {}
    monkeypatch.setattr(jira, "search", lambda q, limit=8, mode="product": seen.setdefault("mode", mode) and [])
    client = FakeClient([
        tool_turn("search_feedback_tracker", {"query": "email"}),
        tool_turn("search_jira", {"query": "email delay"}, tid="t2"),
        text_turn("Email delays are a known P2 issue."),
    ])
    done = list(agent.stream_answer("Any feedback on email delays?", [], client, mode="feedback"))[-1]
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert names == {"search_user_research", "get_user_research_page", "search_feedback_tracker",
                     "search_jira", "get_jira_issue"}
    assert "LPH" in [t for t in client.calls[0]["tools"] if t["name"] == "search_jira"][0]["description"]
    assert "Feedback assistant" in client.calls[0]["system"][0]["text"]
    assert seen["mode"] == "feedback" and done["sources"][0]["kind"] == "sheet"


def test_feedback_jira_projects(monkeypatch):
    monkeypatch.delenv("JIRA_PROJECTS_FEEDBACK", raising=False)
    monkeypatch.setenv("JIRA_PROJECTS", "LP")
    assert jira.projects("feedback") == ["LP", "LPH"] and jira.projects("product") == ["LP"]
    assert jira._allowed_key("LPH-12", jira.projects("feedback")) and not jira._allowed_key("LPH-12", jira.projects())


def test_cs_cannot_use_feedback_tools():
    client = FakeClient([tool_turn("search_feedback_tracker", {"query": "x"}), text_turn(agent.NOT_FOUND_TEXT)])
    agent.answer("x", [], client)
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_feedback_not_found_phrase():
    assert agent.is_not_documented("I couldn't find this in user research, the feedback tracker or Jira.")


def test_notion_client_sends_auth_and_version(monkeypatch):
    c = notion._client()
    assert c.headers["Notion-Version"] == notion.NOTION_VERSION and c.headers["Authorization"] == "Bearer secret_x"
