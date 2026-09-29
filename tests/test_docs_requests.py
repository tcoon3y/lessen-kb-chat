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


def test_row_follows_type_column():
    table5 = TABLE.replace("<th><p>DONE?</p></th>", "<th><p>Type</p></th><th><p>DONE?</p></th>")
    table5 = table5.replace("<td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr>",
                            "<td><p /></td><td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr>")
    hdr = docs_requests.header_names(table5)
    assert hdr == ["subject", "question", "type", "done?", "doc ref"]
    row = docs_requests.build_row("QBO", "Q?", "2026-09-29", "Incorrect answer", hdr)
    assert row == ("<tr><td><p>QBO</p></td><td><p>Q? (asked 2026-09-29)</p></td>"
                   "<td><p>Incorrect answer</p></td><td><p /></td><td><p /></td></tr>")


def test_no_type_column_prefixes_subject():
    row = docs_requests.build_row("QBO", "Q?", "d", "Incorrect answer", docs_requests.header_names(TABLE))
    assert "<p>Incorrect answer: QBO</p>" in row
    assert "<p>Plain</p>" in docs_requests.build_row("Plain", "Q?", "d", "Request", docs_requests.header_names(TABLE))


def test_parse_stats_counts_types_and_done():
    t = ('<table><tbody><tr><th><p>Subject</p></th><th><p>Question</p></th><th><p>Type</p></th>'
         '<th><p>DONE?</p></th><th><p>DOC Ref</p></th></tr>'
         '<tr><td><p>A</p></td><td><p>q</p></td><td><p>Request</p></td><td><p>Yes</p></td><td><p /></td></tr>'
         '<tr><td><p>B</p></td><td><p>q</p></td><td><p>Incorrect answer</p></td><td><p /></td><td><p /></td></tr>'
         '<tr><td><p>C</p></td><td><p>q</p></td><td><p>Not documented</p></td><td><ac:task-list><ac:task>'
         '<ac:task-status>complete</ac:task-status></ac:task></ac:task-list></td><td><p /></td></tr>'
         '<tr><td><p /></td><td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr></tbody></table>')
    st = docs_requests.parse_stats(t)
    assert st["open"] == 1 and st["done"] == 2
    assert st["by_type"]["Incorrect answer"] == {"open": 1, "done": 0}
    assert st["by_type"]["Not documented"] == {"open": 0, "done": 1}


def test_parse_stats_without_type_column_uses_subject_prefix():
    t = TABLE.replace("<tr><td><p /></td><td><p /></td><td><p /></td><td><p /></td></tr>",
                      "<tr><td><p>Incorrect answer: QBO</p></td><td><p>q</p></td><td><p /></td><td><p /></td></tr>", 1)
    st = docs_requests.parse_stats(t)
    assert st["by_type"]["Incorrect answer"]["open"] == 1 and st["by_type"]["Request"]["done"] == 1
