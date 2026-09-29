"""The bot's ONLY Confluence write: add a row to the Documentation Requests table.

Writes are limited to the single page in DOCS_REQUEST_PAGE_ID. It fills the first empty
table row, or appends a new row. It never changes other rows or any other content.
"""
from __future__ import annotations

import html
import re
from datetime import date

import httpx

from app import config, confluence

_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)


class RequestsPageError(Exception):
    pass


def _page_id() -> str:
    pid = config.get("DOCS_REQUEST_PAGE_ID").strip()
    if not pid.isdigit():
        raise RequestsPageError("DOCS_REQUEST_PAGE_ID is not set.")
    return pid


def _cell(text: str) -> str:
    return f"<td><p>{html.escape(text)}</p></td>" if text else "<td><p /></td>"


def header_names(storage: str) -> list[str]:
    """Lower-cased header texts of the first table, e.g. ['subject', 'question', 'type', 'done?', 'doc ref']."""
    for m in _ROW_RE.finditer(storage):
        row = m.group(0)
        if "<th" in row:
            cells = re.findall(r"<th\b[^>]*>(.*?)</th>", row, re.S)
            return [re.sub(r"<[^>]+>|&nbsp;", " ", c).strip().lower() for c in cells]
    return ["subject", "question", "done?", "doc ref"]


def build_row(subject: str, question: str, asked_on: str, kind: str = "Request",
              headers: list[str] | None = None) -> str:
    """Build a row matching the table's columns. Without a Type column, the kind goes in the subject."""
    headers = headers or ["subject", "question", "done?", "doc ref"]
    has_type = any(h.startswith("type") for h in headers)
    if not has_type and kind != "Request":
        subject = f"{kind}: {subject}"
    values = []
    for h in headers:
        if h.startswith("subject"):
            values.append(subject)
        elif h.startswith("question"):
            values.append(f"{question} (asked {asked_on})")
        elif h.startswith("type"):
            values.append(kind)
        else:
            values.append("")
    return "<tr>" + "".join(_cell(v) for v in values) + "</tr>"


def insert_row(storage: str, row: str) -> str:
    """Put row into the first fully empty data row of the first table, else append it."""
    start = storage.find("<table")
    end = storage.find("</table>", start)
    if start == -1 or end == -1:
        raise RequestsPageError("No table found on the requests page.")
    table = storage[start:end]
    for m in _ROW_RE.finditer(table):
        cells = m.group(0)
        if "<th" in cells:
            continue
        if re.sub(r"<[^>]+>|&nbsp;|\s", "", cells) == "":
            new_table = table[:m.start()] + row + table[m.end():]
            return storage[:start] + new_table + storage[end:]
    close = table.rfind("</tbody>")
    new_table = (table[:close] + row + table[close:]) if close != -1 else table + row
    return storage[:start] + new_table + storage[end:]


def _check(r: httpx.Response, what: str) -> None:
    """Raise with Confluence's status and error message (never page content or the question)."""
    if r.status_code < 400:
        return
    try:
        msg = str(r.json().get("message", ""))[:200]
    except Exception:
        msg = ""
    raise RequestsPageError(f"{what} {r.status_code} {msg}".strip())


def add_request(subject: str, question: str, client: httpx.Client | None = None,
                kind: str = "Request") -> None:
    """kind: "Request", "Not documented" or "Incorrect answer" (goes in the Type column if there is one)."""
    subject = " ".join(subject.split())[:80]
    question = " ".join(question.split())[:1000]
    if not question:
        raise RequestsPageError("Empty question.")
    pid = _page_id()
    base = confluence._base_url()
    own = client is None
    client = client or confluence._client()
    try:
        for _attempt in range(3):  # retry if someone edited the page at the same moment
            r = client.get(f"{base}/rest/api/content/{pid}", params={"expand": "body.storage,version"})
            _check(r, "GET")
            page = r.json()
            storage = page["body"]["storage"]["value"]
            new_body = insert_row(storage, build_row(subject, question, date.today().isoformat(),
                                                     kind, header_names(storage)))
            put = client.put(f"{base}/rest/api/content/{pid}", json={
                "id": pid,
                "type": "page",
                "title": page["title"],
                "version": {"number": page["version"]["number"] + 1,
                            "message": "Documentation request from KB bot"},
                "body": {"storage": {"value": new_body, "representation": "storage"}},
            }, headers={"Content-Type": "application/json"})
            if put.status_code == 409:
                continue
            _check(put, "PUT")
            return
        raise RequestsPageError("Page was busy; please try again.")
    finally:
        if own:
            client.close()


_DONE_WORDS = {"yes", "y", "done", "complete", "completed", "true", "x", "✓", "✔", "✅", "closed", "resolved"}
_stats_cache: dict = {"at": 0.0, "data": None}


def _cell_texts(row: str) -> list[str]:
    cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S)
    out = []
    for c in cells:
        if re.search(r"<ac:task-status>\s*complete\s*</ac:task-status>", c):
            out.append("done")
            continue
        out.append(html.unescape(re.sub(r"<[^>]+>|&nbsp;", " ", c)).strip())
    return out


def parse_stats(storage: str) -> dict:
    """Count rows in the requests table by type and status. Counts only, no text."""
    headers = header_names(storage)
    idx = {name: i for i, name in enumerate(headers)}
    col = lambda prefix: next((i for n, i in idx.items() if n.startswith(prefix)), None)
    c_sub, c_type, c_done = col("subject"), col("type"), col("done")
    kinds = ("Request", "Not documented", "Incorrect answer")
    stats = {k: {"open": 0, "done": 0} for k in kinds}
    table = storage[storage.find("<table"):storage.find("</table>")] if "<table" in storage else ""
    for m in _ROW_RE.finditer(table):
        row = m.group(0)
        if "<th" in row:
            continue
        cells = _cell_texts(row)
        if not any(cells):
            continue
        kind = "Request"
        tval = cells[c_type] if c_type is not None and c_type < len(cells) else ""
        sval = cells[c_sub] if c_sub is not None and c_sub < len(cells) else ""
        for k in kinds[1:]:
            if tval.lower().startswith(k.lower()) or sval.lower().startswith(k.lower() + ":"):
                kind = k
        dval = (cells[c_done] if c_done is not None and c_done < len(cells) else "").strip().lower()
        stats[kind]["done" if dval in _DONE_WORDS or dval.startswith("done") else "open"] += 1
    total_open = sum(v["open"] for v in stats.values())
    total_done = sum(v["done"] for v in stats.values())
    return {"by_type": stats, "open": total_open, "done": total_done}


def stats(client: httpx.Client | None = None, max_age: float = 60.0) -> dict:
    """Live counts from the requests page (cached for a minute)."""
    import time
    if _stats_cache["data"] is not None and time.monotonic() - _stats_cache["at"] < max_age:
        return _stats_cache["data"]
    pid = _page_id()
    base = confluence._base_url()
    own = client is None
    client = client or confluence._client()
    try:
        r = client.get(f"{base}/rest/api/content/{pid}", params={"expand": "body.storage"})
        _check(r, "GET")
        data = parse_stats(r.json()["body"]["storage"]["value"])
    finally:
        if own:
            client.close()
    _stats_cache.update(at=time.monotonic(), data=data)
    return data


if __name__ == "__main__":
    import sys
    add_request("Test request", " ".join(sys.argv[1:]) or "Test question from the diagnostic CLI")
    print("OK: row added to the Documentation Requests page.")
