"""Read-only Confluence access, restricted to ALLOWED_SPACES in code.

Only GET requests are made. Anything outside ALLOWED_SPACES is treated as not found.
"""
from __future__ import annotations

import re
import sys
from html import unescape
from html.parser import HTMLParser

import httpx

from app import config

MAX_PAGE_CHARS = 20_000
DEFAULT_LIMIT = 8
MAX_LIMIT = 15
TIMEOUT = 10.0
_SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_~-]+$")


class PageNotFound(Exception):
    """Page missing, unreadable, or outside ALLOWED_SPACES."""


# ---------- helpers ----------

def _base_url() -> str:
    return config.require("CONFLUENCE_BASE_URL").rstrip("/")


def _spaces() -> list[str]:
    spaces = [s for s in config.allowed_spaces() if _SPACE_KEY_RE.match(s)]
    if not spaces:
        raise RuntimeError("ALLOWED_SPACES is empty or invalid.")
    return spaces


def _count(response: httpx.Response) -> None:
    from app import usage  # local import avoids a cycle
    service = "notion" if "notion.com" in response.request.url.host else "atlassian"
    usage.record_confluence(response.status_code, response.headers, service)


def _client() -> httpx.Client:
    return httpx.Client(
        auth=(config.require("CONFLUENCE_EMAIL"), config.require("CONFLUENCE_API_TOKEN")),
        timeout=TIMEOUT,
        headers={"Accept": "application/json"},
        event_hooks={"response": [_count]},
    )


def _cql_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_cql(query: str, spaces: list[str]) -> str:
    space_list = ",".join(_cql_quote(s) for s in spaces)
    return f"siteSearch ~ {_cql_quote(query)} AND space IN ({space_list}) AND type = page"


def _excluded_ids() -> set[str]:
    """Pages the bot must never read or cite (e.g. the Documentation Requests list)."""
    return {config.get("DOCS_REQUEST_PAGE_ID").strip(),
            (config.get("FEEDBACK_PAGE_ID") or "3971678223").strip()} - {""}


def _allowed(space_key: str | None, spaces: list[str]) -> bool:
    return bool(space_key) and space_key.lower() in {s.lower() for s in spaces}


def _clean_excerpt(text: str) -> str:
    text = text.replace("@@@hl@@@", "").replace("@@@endhl@@@", "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "ul", "ol", "blockquote", "pre", "hr"}
    SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def unknown_decl(self, data):  # CDATA inside code macros
        if data.startswith("CDATA["):
            self.parts.append(data[6:])


def html_to_text(html: str, limit: int = MAX_PAGE_CHARS) -> str:
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


# ---------- public API ----------

def search(query: str, limit: int = DEFAULT_LIMIT, client: httpx.Client | None = None) -> list[dict]:
    """Search allowed spaces. Returns title, page_id, space, last_updated, url, excerpt."""
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    spaces = _spaces()
    base = _base_url()
    params = {"cql": build_cql(query, spaces), "limit": limit, "expand": "content.space,content.version"}
    own = client is None
    client = client or _client()
    try:
        r = client.get(f"{base}/rest/api/search", params=params)
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            client.close()

    hits = []
    for item in data.get("results", []):
        content = item.get("content") or {}
        space_key = (content.get("space") or {}).get("key")
        if content.get("type") != "page" or not _allowed(space_key, spaces):
            continue
        if str(content.get("id", "")) in _excluded_ids():
            continue  # defence in depth: drop anything outside allowed spaces
        webui = (content.get("_links") or {}).get("webui") or item.get("url", "")
        hits.append({
            "title": content.get("title") or item.get("title", ""),
            "page_id": str(content.get("id", "")),
            "space": space_key,
            "last_updated": item.get("lastModified") or (content.get("version") or {}).get("when", ""),
            "url": base + webui,
            "excerpt": _clean_excerpt(item.get("excerpt", "")),
        })
    return hits[:limit]


def get_page(page_id: str, client: httpx.Client | None = None) -> dict:
    """Read one page as plain text. Raises PageNotFound if missing or outside ALLOWED_SPACES."""
    page_id = str(page_id).strip()
    if not page_id.isdigit() or page_id in _excluded_ids():
        raise PageNotFound(page_id)
    spaces = _spaces()
    base = _base_url()
    own = client is None
    client = client or _client()
    try:
        r = client.get(f"{base}/rest/api/content/{page_id}",
                       params={"expand": "body.storage,space,version"})
        if r.status_code in (403, 404):
            raise PageNotFound(page_id)
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            client.close()

    space_key = (data.get("space") or {}).get("key")
    if data.get("type") != "page" or not _allowed(space_key, spaces):
        raise PageNotFound(page_id)
    return {
        "title": data.get("title", ""),
        "page_id": page_id,
        "space": space_key,
        "url": base + ((data.get("_links") or {}).get("webui") or ""),
        "last_updated": (data.get("version") or {}).get("when", ""),
        "text": html_to_text(((data.get("body") or {}).get("storage") or {}).get("value", "")),
    }


def whoami(client: httpx.Client | None = None) -> str | None:
    """Display name of the logged-in Confluence user, or None if the login failed."""
    own = client is None
    client = client or _client()
    try:
        r = client.get(f"{_base_url()}/rest/api/user/current")
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("type") == "anonymous" or not data.get("accountId"):
        return None
    return data.get("displayName") or data.get("publicName") or "unknown user"


def diagnose() -> None:
    """Print safe login diagnostics. Never prints the email or token themselves."""
    email = config.get("CONFLUENCE_EMAIL")
    token = config.get("CONFLUENCE_API_TOKEN")
    print("Diagnostics (no secret values shown):")
    print(f"  base URL: {config.get('CONFLUENCE_BASE_URL')}")
    print(f"  email domain: @{email.split('@')[-1] if '@' in email else '(no @ found!)'}")
    print(f"  email has spaces/quotes: {any(c in email for c in ' \t\"\'')}")
    print(f"  token length: {len(token)} (a classic token is usually about 190-200)")
    print(f"  token starts with ATATT: {token.startswith('ATATT')}")
    print(f"  token has spaces/quotes: {any(c in token for c in ' \t\"\'')}")
    with _client() as client:
        r = client.get(f"{_base_url()}/rest/api/user/current")
    print(f"  Atlassian response: HTTP {r.status_code}")
    body = r.text[:300].replace("\n", " ")
    if r.status_code != 200 or '"anonymous"' in body:
        print(f"  Atlassian says: {body}")


_login_cache: dict = {"ok": None, "at": 0.0}


def login_ok(max_age: float = 600.0) -> bool:
    """True if the Confluence credentials are accepted (cached for 10 minutes)."""
    import time
    now = time.monotonic()
    if _login_cache["ok"] is not None and now - _login_cache["at"] < max_age:
        return _login_cache["ok"]
    try:
        ok = whoami() is not None
    except Exception:
        ok = False
    _login_cache.update(ok=ok, at=now)
    return ok


def _cli() -> None:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print('Usage: uv run python -m app.confluence "free tier"')
        raise SystemExit(1)
    user = whoami()
    if not user:
        print("NOT LOGGED IN to Confluence. Check CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN in .env")
        print("(use a classic API token, 'Create API token', not 'Create API token with scopes').\n")
        diagnose()
        raise SystemExit(1)
    print(f"Logged in as {user}. Allowed spaces: {', '.join(_spaces())}\n")
    hits = search(query)
    if not hits:
        print("No results in allowed spaces.")
        return
    for i, h in enumerate(hits, 1):
        print(f"{i}. [{h['space']}] {h['title']}  (updated {h['last_updated'][:10]})")
        print(f"   {h['url']}")


if __name__ == "__main__":
    _cli()
