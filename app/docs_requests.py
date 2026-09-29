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


def build_row(subject: str, question: str, asked_on: str) -> str:
    return ("<tr>" + _cell(subject) + _cell(f"{question} (asked {asked_on})")
            + _cell("") + _cell("") + "</tr>")


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


def add_request(subject: str, question: str, client: httpx.Client | None = None) -> None:
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
            r.raise_for_status()
            page = r.json()
            new_body = insert_row(page["body"]["storage"]["value"],
                                  build_row(subject, question, date.today().isoformat()))
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
            put.raise_for_status()
            return
        raise RequestsPageError("Page was busy; please try again.")
    finally:
        if own:
            client.close()
