import httpx
import pytest

from app import confluence

BASE = "https://example.atlassian.net/wiki"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", BASE)
    monkeypatch.setenv("CONFLUENCE_EMAIL", "test@example.com")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "dummy")
    monkeypatch.setenv("ALLOWED_SPACES", "VendorSaaS,PM")


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def hit(pid, title, space, ctype="page"):
    return {
        "content": {"id": pid, "type": ctype, "title": title, "space": {"key": space},
                    "_links": {"webui": f"/spaces/{space}/pages/{pid}"}},
        "title": title, "excerpt": "The @@@hl@@@free tier@@@endhl@@@ includes &amp; more",
        "lastModified": "2026-09-01T10:00:00.000Z",
    }


def test_cql_restricts_spaces_and_escapes_quotes():
    cql = confluence.build_cql('say "hi"', ["VendorSaaS", "PM"])
    assert cql == 'siteSearch ~ "say \\"hi\\"" AND space IN ("VendorSaaS","PM") AND type = page'


def test_search_sends_allowed_spaces_and_filters_results():
    seen = {}

    def handler(req):
        seen["cql"] = req.url.params["cql"]
        seen["limit"] = req.url.params["limit"]
        seen["method"] = req.method
        return httpx.Response(200, json={"results": [
            hit("1", "Free tier overview", "VendorSaaS"),
            hit("2", "Secret HR page", "HR"),          # not allowed
            hit("3", "A blog post", "PM", "blogpost"),  # not a page
            hit("4", "PM roadmap", "pm"),               # case-insensitive match
        ]})

    hits = confluence.search("free tier", limit=50, client=mock_client(handler))
    assert seen["method"] == "GET"
    assert 'space IN ("VendorSaaS","PM")' in seen["cql"]
    assert seen["limit"] == "15"  # capped at max
    assert [h["page_id"] for h in hits] == ["1", "4"]
    assert hits[0]["url"] == f"{BASE}/spaces/VendorSaaS/pages/1"
    assert hits[0]["excerpt"] == "The free tier includes & more"
    assert hits[0]["last_updated"].startswith("2026-09-01")


def test_search_empty_query_makes_no_call():
    def handler(req):
        raise AssertionError("should not call")
    assert confluence.search("  ", client=mock_client(handler)) == []


def page_json(space, body="<p>Hello <b>world</b></p>"):
    return {"id": "123", "type": "page", "title": "Free tier", "space": {"key": space},
            "version": {"when": "2026-09-02T00:00:00.000Z"},
            "_links": {"webui": "/spaces/X/pages/123"},
            "body": {"storage": {"value": body}}}


def test_get_page_allowed():
    page = confluence.get_page("123", client=mock_client(lambda r: httpx.Response(200, json=page_json("PM"))))
    assert page["title"] == "Free tier"
    assert page["text"] == "Hello world"
    assert page["url"] == f"{BASE}/spaces/X/pages/123"


def test_get_page_outside_allowed_spaces_is_not_found():
    with pytest.raises(confluence.PageNotFound):
        confluence.get_page("123", client=mock_client(lambda r: httpx.Response(200, json=page_json("HR"))))


def test_get_page_404_and_bad_id():
    with pytest.raises(confluence.PageNotFound):
        confluence.get_page("123", client=mock_client(lambda r: httpx.Response(404)))
    with pytest.raises(confluence.PageNotFound):
        confluence.get_page("../admin", client=mock_client(lambda r: httpx.Response(200)))


def test_html_to_text_structure_and_cap():
    html = ("<h2>Plans</h2><ul><li>Free</li><li>Pro</li></ul><script>x()</script>"
            "<table><tr><td>A</td><td>B</td></tr></table>"
            "<ac:structured-macro><ac:plain-text-body><![CDATA[code here]]></ac:plain-text-body></ac:structured-macro>")
    text = confluence.html_to_text(html)
    assert "Plans" in text and "- Free" in text and "- Pro" in text
    assert "x()" not in text
    assert "| A | B" in text
    assert "code here" in text
    assert len(confluence.html_to_text("<p>" + "a" * 30000 + "</p>")) == 20_000


def test_whoami_detects_anonymous():
    anon = mock_client(lambda r: httpx.Response(200, json={"type": "anonymous"}))
    assert confluence.whoami(client=anon) is None
    bad = mock_client(lambda r: httpx.Response(401))
    assert confluence.whoami(client=bad) is None
    ok = mock_client(lambda r: httpx.Response(200, json={"type": "known", "accountId": "a1", "displayName": "Thomas"}))
    assert confluence.whoami(client=ok) == "Thomas"
