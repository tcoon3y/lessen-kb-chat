"""Search the CS feedback tracker, mirrored daily from Excel to a Confluence page (FEEDBACK_PAGE_ID).

The page has one "## <sheet>" heading plus one table per worksheet. Rows are parsed into
dicts and searched by keyword. Read-only; cached for 10 minutes.
"""
from __future__ import annotations

import html
import re
import threading
import time

import httpx

from app import config, confluence

CACHE_SECONDS = 600
DEFAULT_PAGE = "3971678223"
EXCEL_URL = ("https://lessenllc.sharepoint.com/sites/ProductTeamUS2/Shared%20Documents/"
             "Lessen%20Pro/Lessen%20Pro%20Feedback.xlsx")
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def page_id() -> str:
    return (config.get("FEEDBACK_PAGE_ID") or DEFAULT_PAGE).strip()


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>|&nbsp;", " ", cell))).strip()


def parse(storage: str) -> list[dict]:
    """[{sheet, headers, rows: [dict]}] from the mirrored page."""
    sheets = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>\s*(?:<[^t][^>]*>\s*)*?(<table\b.*?</table>)", storage, re.S):
        name, table = _text(m.group(1)), m.group(2)
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S)
        if not rows:
            continue
        headers = [_text(c) or f"Col {i + 1}" for i, c in
                   enumerate(re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rows[0], re.S))]
        out = []
        for r in rows[1:]:
            cells = [_text(c) for c in re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", r, re.S)]
            row = {h: c for h, c in zip(headers, cells) if c}
            if row:
                out.append(row)
        sheets.append({"sheet": name, "headers": headers, "rows": out})
    return sheets


def load(client: httpx.Client | None = None) -> dict:
    with _lock:
        if _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    own = client is None
    client = client or confluence._client()
    try:
        r = client.get(f"{confluence._base_url()}/rest/api/content/{page_id()}",
                       params={"expand": "body.storage,version"})
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            client.close()
    result = {"sheets": parse(data["body"]["storage"]["value"]),
              "synced": (data.get("version") or {}).get("when", "")}
    with _lock:
        _cache.update(at=time.monotonic(), data=result)
    return result


def search(query: str = "", sheet: str = "", limit: int = 25, client: httpx.Client | None = None) -> dict:
    data = load(client)
    sheets = [s for s in data["sheets"] if not sheet or s["sheet"].lower() == sheet.strip().lower()]
    terms = [t for t in re.findall(r"[a-z0-9#]+", (query or "").lower()) if len(t) > 1]
    if not terms:  # overview
        overview = []
        for s in sheets:
            info = {"sheet": s["sheet"], "rows": len(s["rows"]), "columns": s["headers"]}
            for col in ("Status", "Priority"):
                if col in s["headers"]:
                    counts: dict[str, int] = {}
                    for r in s["rows"]:
                        v = (r.get(col) or "blank").strip().title()
                        counts[v] = counts.get(v, 0) + 1
                    info[f"by_{col.lower()}"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
            overview.append(info)
        return {"synced": data["synced"], "overview": overview, "source_url": EXCEL_URL}
    hits = []
    for s in sheets:
        for i, r in enumerate(s["rows"]):
            blob = " ".join(r.values()).lower()
            score = sum(blob.count(t) for t in terms)
            if score:
                hits.append((score, {"sheet": s["sheet"], "row": i + 2, **r}))
    hits.sort(key=lambda h: -h[0])
    return {"synced": data["synced"], "matches": [h for _, h in hits[:max(1, min(limit, 40))]],
            "total_matches": len(hits), "source_url": EXCEL_URL}


def _cli() -> None:
    import sys
    query = " ".join(sys.argv[1:]).strip()
    with confluence._client() as client:
        r = client.get(f"{confluence._base_url()}/rest/api/content/{page_id()}", params={"expand": "body.storage,version"})
    print(f"Feedback page {page_id()}: HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300]); raise SystemExit(1)
    storage = r.json()["body"]["storage"]["value"]
    sheets = parse(storage)
    print(f"Page size: {len(storage):,} chars · h2 headings: {storage.count('<h2')} · tables: {storage.count('<table')}")
    print(f"Parsed sheets: {len(sheets)}")
    for s in sheets:
        print(f"  - {s['sheet']}: {len(s['rows'])} rows, columns: {', '.join(s['headers'][:6])}")
    if not sheets:
        i = storage.find("<h2")
        print("\nStructure sample (tags only):", re.sub(r">[^<]{1,}<", "><", storage[i:i + 600]))
    if query:
        _cache.update(at=0.0, data=None)
        res = search(query)
        print(f"\nSearch '{query}': {res.get('total_matches', 0)} matches")
        for m in res.get("matches", [])[:5]:
            print("  ", {k: v for k, v in list(m.items())[:4]})


if __name__ == "__main__":
    _cli()
