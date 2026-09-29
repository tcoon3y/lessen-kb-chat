import httpx
import pytest

from app import confluence, docs_requests

TABLE = ('<p>intro</p><table><tbody><tr><th><p>Subject</p></th><th><p>Question</p></th>'
         '<th><p>DONE?</p></th><th><p>DOC Ref</p></th></tr>'
         '<tr><td><p>Old</p></td><td><p>Old q</p></td><td><p>Yes</p></td><td><p /></td></tr>'
         '<tr><td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr>'
         '<tr><td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr></tbody></table><p>after</p>')


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://x/wiki")
    monkeypatch.setenv("CONFLUENCE_EMAIL", "e")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_SPACES", "TCN")
    monkeypatch.setenv("DOCS_REQUEST_PAGE_ID", "42")


def test_fills_first_empty_row_and_escapes():
    row = docs_requests.build_row("Subj", "Is <b>x</b> & y?", "2026-09-29")
    out = docs_requests.insert_row(TABLE, row)
    assert out.count("<tr>") == TABLE.count("<tr>")          # no new row, filled an empty one
    assert "Old q" in out and "<p>intro</p>" in out and "<p>after</p>" in out
    assert "Is &lt;b&gt;x&lt;/b&gt; &amp; y? (asked 2026-09-29)" in out
    assert out.index("Subj") < out.index("<tr><td><p /></td>")  # the later empty row is still there


def test_appends_when_no_empty_rows():
    full = TABLE.replace('<tr><td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr>', "")
    out = docs_requests.insert_row(full, docs_requests.build_row("S", "Q", "d"))
    assert out.count("<tr>") == full.count("<tr>") + 1
    assert out.index("<p>S</p>") < out.index("</tbody>")


def test_writes_only_configured_page_and_retries_conflict():
    seen, puts = [], []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(200, json={"title": "Documentation Requests", "version": {"number": 5},
                                             "body": {"storage": {"value": TABLE}}})
        puts.append(req)
        return httpx.Response(409) if len(puts) == 1 else httpx.Response(200, json={})

    docs_requests.add_request("Subj", "A question", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert {p for _, p in seen} == {"/wiki/rest/api/content/42"}
    assert len(puts) == 2


def test_requires_page_id(monkeypatch):
    monkeypatch.setenv("DOCS_REQUEST_PAGE_ID", "")
    with pytest.raises(docs_requests.RequestsPageError):
        docs_requests.add_request("s", "q", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))


def test_requests_page_is_never_read_or_cited():
    def handler(req):
        return httpx.Response(200, json={"results": [
            {"content": {"id": "42", "type": "page", "title": "Documentation Requests", "space": {"key": "TCN"},
                         "_links": {"webui": "/x"}}},
            {"content": {"id": "7", "type": "page", "title": "Real doc", "space": {"key": "TCN"},
                         "_links": {"webui": "/y"}}}]})
    hits = confluence.search("anything", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [h["page_id"] for h in hits] == ["7"]
    with pytest.raises(confluence.PageNotFound):
        confluence.get_page("42", client=httpx.Client(transport=httpx.MockTransport(handler)))
