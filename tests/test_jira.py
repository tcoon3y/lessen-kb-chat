import json

import httpx
import pytest

from app import jira


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://lessen.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_EMAIL", "e")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "t")
    monkeypatch.setenv("JIRA_PROJECTS", "LP")
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)


def mc(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def issue(key, summary="S", status="In Progress"):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status}, "issuetype": {"name": "Story"},
                                   "updated": "2026-09-20T10:00:00.000+0000", "fixVersions": [{"name": "2.4"}]}}


def test_base_url_and_jql():
    assert jira.base_url() == "https://lessen.atlassian.net"
    assert jira.build_jql('say "hi"', ["LP"]) == 'project in (LP) AND text ~ "say \\"hi\\"" ORDER BY updated DESC'


def test_search_restricts_project_and_filters():
    seen = {}

    def handler(req):
        seen["method"], seen["path"] = req.method, req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"issues": [issue("LP-1", "Autopay"), issue("HR-9", "Salary")]})

    rows = jira.search("autopay", limit=50, client=mc(handler))
    assert seen["method"] == "POST" and seen["path"] == "/rest/api/3/search/jql"
    assert seen["body"]["jql"].startswith("project in (LP) AND") and seen["body"]["maxResults"] == 15
    assert [r["key"] for r in rows] == ["LP-1"]
    assert rows[0]["url"] == "https://lessen.atlassian.net/browse/LP-1" and rows[0]["fix_versions"] == ["2.4"]


def test_get_issue_blocks_other_projects_without_calling():
    def handler(req):
        raise AssertionError("should not call")
    for key in ("LPH-5", "HR-1", "../x", ""):
        with pytest.raises(jira.IssueNotFound):
            jira.get_issue(key, client=mc(handler))


def test_get_issue_text_and_comments():
    def handler(req):
        data = issue("LP-7", "Stripe autopay")
        data["fields"]["comment"] = {"comments": [{"author": {"displayName": "Wilson"}, "created": "2026-09-21T01:00:00.000+0000"}]}
        data["renderedFields"] = {"description": "<p>Gate behind <b>OTP</b></p>",
                                  "comment": {"comments": [{"body": "<p>Blocked on data exposure</p>"}]}}
        return httpx.Response(200, json=data)
    got = jira.get_issue("lp-7", client=mc(handler))
    assert got["key"] == "LP-7" and "Gate behind OTP" in got["text"]
    assert "Comment by Wilson on 2026-09-21" in got["text"] and "Blocked on data exposure" in got["text"]


def test_moved_issue_is_not_found():
    with pytest.raises(jira.IssueNotFound):
        jira.get_issue("LP-7", client=mc(lambda r: httpx.Response(200, json=issue("OPS-3"))))
