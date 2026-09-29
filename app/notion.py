"""Read-only Notion access for the User feedback chat, limited to one page tree (User Research).

Notion's API search only matches page titles, so the bot keeps a small in-memory copy of the
text of every page under NOTION_ROOT_PAGE_ID (refreshed every 30 minutes in the background)
and searches that. Pages outside the tree are never read.
"""
from __future__ import annotations

import re
import sys
import threading
import time

import httpx

from app import config

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_ROOT = "328878319aeb80f1839ffb8a08f4a609"  # ♥️ User Research
REFRESH_SECONDS = 30 * 60
MAX_PAGES = 300
MAX_PAGE_CHARS = 60_000
MAX_READ_CHARS = 20_000
MAX_DEPTH = 6
TEXT_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
              "to_do", "toggle", "quote", "callout", "code", "transcription")


class NotionNotConfigured(Exception):
    pass


class NotionPageNotFound(Exception):
    pass


def enabled() -> bool:
    return bool(config.get("NOTION_TOKEN").strip())


def _root() -> str:
    return _norm_id(config.get("NOTION_ROOT_PAGE_ID") or DEFAULT_ROOT)


def _norm_id(pid: str) -> str:
    m = re.search(r"([0-9a-f]{32})", (pid or "").replace("-", "").lower())
    return m.group(1) if m else ""


def _url(pid: str) -> str:
    return f"https://www.notion.so/{pid}"


def _client() -> httpx.Client:
    token = config.get("NOTION_TOKEN").strip()
    if not token:
        raise NotionNotConfigured()
    from app.confluence import _count  # same usage counter hook
    return httpx.Client(timeout=15.0, headers={"Authorization": f"Bearer {token}",
                                               "Notion-Version": NOTION_VERSION},
                        event_hooks={"response": [_count]})


def _rich(items) -> str:
    return "".join(t.get("plain_text", "") for t in items or [])


def _children(client: httpx.Client, block_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        for attempt in range(3):
            r = client.get(f"{API}/blocks/{block_id}/children", params=params)
            if r.status_code != 429:
                break
            time.sleep(float(r.headers.get("retry-after", "1")))
        r.raise_for_status()
        data = r.json()
        out += data.get("results", [])
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")


def _db_pages(client: httpx.Client, db_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = client.post(f"{API}/databases/{db_id}/query", json=body)
        r.raise_for_status()
        data = r.json()
        out += data.get("results", [])
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _rich(prop.get("title")) or "Untitled"
    return "Untitled"


class _Crawler:
    def __init__(self, client: httpx.Client):
        self.client = client
        self.pages: dict[str, dict] = {}

    def page(self, pid: str, title: str, edited: str, path: list[str], depth: int) -> None:
        if pid in self.pages or len(self.pages) >= MAX_PAGES or depth > MAX_DEPTH:
            return
        entry = {"id": pid, "title": title, "path": " / ".join(path), "url": _url(pid),
                 "last_updated": edited, "text": ""}
        self.pages[pid] = entry
        parts: list[str] = []
        self.blocks(pid, parts, path + [title], depth, 0)
        entry["text"] = "\n".join(p for p in parts if p.strip())[:MAX_PAGE_CHARS]

    def blocks(self, block_id: str, parts: list[str], path: list[str], depth: int, nest: int) -> None:
        for b in _children(self.client, block_id):
            t = b.get("type")
            if t == "child_page":
                self.page(_norm_id(b["id"]), b["child_page"].get("title") or "Untitled",
                          b.get("last_edited_time", ""), path, depth + 1)
                continue
            if t == "child_database":
                for row in _db_pages(self.client, b["id"]):
                    self.page(_norm_id(row["id"]), _page_title(row), row.get("last_edited_time", ""),
                              path + [b["child_database"].get("title") or "Database"], depth + 1)
                continue
            data = b.get(t) or {}
            text = _rich(data.get("rich_text"))
            if t in ("heading_1", "heading_2", "heading_3") and text:
                text = "## " + text
            elif t in ("bulleted_list_item", "numbered_list_item", "to_do") and text:
                text = "- " + text
            if text:
                parts.append(text)
            if b.get("has_children") and nest < 4 and t not in ("child_page", "child_database"):
                self.blocks(b["id"], parts, path, depth, nest + 1)


_lock = threading.Lock()
_state: dict = {"pages": {}, "at": 0.0, "error": None, "running": False}


def refresh(client: httpx.Client | None = None) -> None:
    """Re-crawl the research tree. Safe to call from a background thread."""
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    own = client is None
    try:
        client = client or _client()
        root = _root()
        r = client.get(f"{API}/pages/{root}")
        if r.status_code in (401, 403, 404):
            raise PermissionError(f"Notion HTTP {r.status_code}")
        r.raise_for_status()
        crawler = _Crawler(client)
        crawler.page(root, _page_title(r.json()), r.json().get("last_edited_time", ""), [], 0)
        with _lock:
            _state.update(pages=crawler.pages, at=time.monotonic(), error=None)
    except Exception as exc:  # keep the last good copy
        with _lock:
            _state["error"] = type(exc).__name__ + (f": {exc}" if isinstance(exc, PermissionError) else "")
    finally:
        with _lock:
            _state["running"] = False
        if own and client is not None:
            client.close()


def _ensure_fresh(wait: float = 45.0) -> None:
    if not enabled():
        raise NotionNotConfigured()
    stale = time.monotonic() - _state["at"] > REFRESH_SECONDS
    if not _state["pages"]:
        refresh()  # first use: crawl now
    elif stale and not _state["running"]:
        threading.Thread(target=refresh, daemon=True).start()


def start_background() -> None:
    if enabled():
        threading.Thread(target=refresh, daemon=True).start()


def status() -> dict:
    return {"enabled": enabled(), "pages": len(_state["pages"]), "error": _state["error"]}


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) > 2]


def search(query: str, limit: int = 8) -> list[dict]:
    _ensure_fresh()
    if not _state["pages"] and _state["error"]:
        raise PermissionError(_state["error"])
    terms = _terms(query)
    scored = []
    for p in _state["pages"].values():
        title, text = p["title"].lower(), p["text"].lower()
        score = sum(title.count(t) * 5 + p["path"].lower().count(t) * 2 + min(text.count(t), 20) for t in terms)
        if score:
            first = min((text.find(t) for t in terms if t in text), default=0)
            snippet = p["text"][max(0, first - 150): first + 250].replace("\n", " ")
            scored.append((score, {"page_id": p["id"], "title": p["title"], "path": p["path"],
                                   "last_updated": p["last_updated"], "url": p["url"], "excerpt": snippet}))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:max(1, min(limit, 15))]]


def get_page(page_id: str) -> dict:
    _ensure_fresh()
    p = _state["pages"].get(_norm_id(page_id))
    if not p:
        raise NotionPageNotFound(page_id)
    return {**p, "text": p["text"][:MAX_READ_CHARS]}


def _cli() -> None:
    query = " ".join(sys.argv[1:]).strip()
    refresh()
    st = status()
    print(f"Notion: {st['pages']} pages under the research root" + (f" (error: {st['error']})" if st["error"] else ""))
    if st["error"]:
        print("Check NOTION_TOKEN and that the User Research page is shared with your Notion integration.")
        raise SystemExit(1)
    for p in list(_state["pages"].values())[:40]:
        print(f"  - {p['path'] + ' / ' if p['path'] else ''}{p['title']}  ({len(p['text'])} chars)")
    if query:
        print(f"\nSearch: {query}")
        for i, h in enumerate(search(query), 1):
            print(f"{i}. {h['title']}  {h['url']}\n   …{h['excerpt'][:160]}…")


if __name__ == "__main__":
    _cli()
