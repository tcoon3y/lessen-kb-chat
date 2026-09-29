"""Read-only Jira search for the Product chat, restricted to JIRA_PROJECTS in code.

Uses the same Atlassian email + API token as Confluence. Only GET/search requests are made.
Issues outside the allowed projects are treated as not found.
"""
from __future__ import annotations

import re
import sys

import httpx

from app import config, confluence

MAX_ISSUE_CHARS = 20_000
DEFAULT_LIMIT = 8
MAX_LIMIT = 15
MAX_COMMENTS = 8
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
SEARCH_FIELDS = ["summary", "status", "issuetype", "updated", "fixVersions", "parent", "resolution"]


class IssueNotFound(Exception):
    """Issue missing, unreadable, or outside JIRA_PROJECTS."""


def base_url() -> str:
    """Jira lives at the site root: https://lessen.atlassian.net (Confluence adds /wiki)."""
    explicit = config.get("JIRA_BASE_URL").rstrip("/")
    if explicit:
        return explicit
    return re.sub(r"/wiki/?$", "", config.require("CONFLUENCE_BASE_URL").rstrip("/"))


def projects(mode: str = "product") -> list[str]:
    raw = (config.get("JIRA_PROJECTS_FEEDBACK", "LP,LPH") if mode == "feedback"
           else config.get("JIRA_PROJECTS", "LP"))
    keys = [p.strip().upper() for p in raw.split(",") if p.strip()]
    keys = [k for k in keys if _PROJECT_RE.match(k)]
    if not keys:
        raise RuntimeError("JIRA_PROJECTS is empty or invalid.")
    return keys


def enabled() -> bool:
    return bool(config.get("JIRA_PROJECTS", "LP").strip())


def _allowed_key(key: str, keys: list[str]) -> bool:
    m = re.match(r"^([A-Z][A-Z0-9_]+)-\d+$", key or "")
    return bool(m) and m.group(1) in keys


def _jql_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_jql(query: str, keys: list[str]) -> str:
    return f"project in ({', '.join(keys)}) AND text ~ {_jql_quote(query)} ORDER BY updated DESC"


def _name(obj: dict | None, key: str = "name") -> str:
    return (obj or {}).get(key, "") if isinstance(obj, dict) else ""


def _summary_row(issue: dict, base: str) -> dict:
    f = issue.get("fields") or {}
    parent = f.get("parent") or {}
    return {
        "key": issue.get("key", ""),
        "summary": f.get("summary", ""),
        "type": _name(f.get("issuetype")),
        "status": _name(f.get("status")),
        "resolution": _name(f.get("resolution")),
        "fix_versions": [v.get("name", "") for v in f.get("fixVersions") or []],
        "parent": f"{parent.get('key', '')} {_name(parent.get('fields'), 'summary')}".strip(),
        "last_updated": f.get("updated", ""),
        "url": f"{base}/browse/{issue.get('key', '')}",
    }


def search(query: str, limit: int = DEFAULT_LIMIT, client: httpx.Client | None = None,
           mode: str = "product") -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    keys = projects(mode)
    base = base_url()
    own = client is None
    client = client or confluence._client()
    try:
        r = client.post(f"{base}/rest/api/3/search/jql",
                        json={"jql": build_jql(query, keys), "maxResults": limit, "fields": SEARCH_FIELDS})
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            client.close()
    rows = [_summary_row(i, base) for i in data.get("issues", []) if _allowed_key(i.get("key", ""), keys)]
    return rows[:limit]


def get_issue(key: str, client: httpx.Client | None = None, mode: str = "product") -> dict:
    key = (key or "").strip().upper()
    keys = projects(mode)
    if not _allowed_key(key, keys):
        raise IssueNotFound(key)
    base = base_url()
    own = client is None
    client = client or confluence._client()
    try:
        r = client.get(f"{base}/rest/api/3/issue/{key}", params={
            "fields": ",".join(SEARCH_FIELDS + ["description", "comment", "priority", "labels", "assignee"]),
            "expand": "renderedFields"})
        if r.status_code in (403, 404):
            raise IssueNotFound(key)
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            client.close()
    if not _allowed_key(data.get("key", ""), keys):  # e.g. an issue moved to another project
        raise IssueNotFound(key)
    row = _summary_row(data, base)
    f, rf = data.get("fields") or {}, data.get("renderedFields") or {}
    parts = [confluence.html_to_text(rf.get("description") or "", MAX_ISSUE_CHARS)]
    comments = ((rf.get("comment") or {}).get("comments") or [])[-MAX_COMMENTS:]
    raw_comments = ((f.get("comment") or {}).get("comments") or [])[-MAX_COMMENTS:]
    for i, c in enumerate(comments):
        raw = raw_comments[i] if i < len(raw_comments) else {}
        author = _name(raw.get("author"), "displayName")
        when = str(raw.get("created") or "")[:10]
        parts.append(f"--- Comment{(' by ' + author) if author else ''}{(' on ' + when) if when else ''}:\n"
                     + confluence.html_to_text(c.get("body") or "", 3000))
    row.update({
        "priority": _name(f.get("priority")),
        "labels": f.get("labels") or [],
        "assignee": _name(f.get("assignee"), "displayName"),
        "text": "\n\n".join(p for p in parts if p)[:MAX_ISSUE_CHARS],
    })
    return row


def diagnose(client: httpx.Client | None = None) -> str | None:
    """Print who Jira thinks we are. Returns the display name, or None if the login wasn't accepted."""
    own = client is None
    client = client or confluence._client()
    try:
        r = client.get(f"{base_url()}/rest/api/3/myself")
    finally:
        if own:
            client.close()
    print(f"Jira: {base_url()}  projects: {', '.join(projects())}")
    if r.status_code != 200:
        print(f"NOT LOGGED IN to Jira (HTTP {r.status_code}): {r.text[:200]}")
        return None
    name = r.json().get("displayName") or "unknown user"
    print(f"Logged in to Jira as {name}")
    return name


def _cli() -> None:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print('Usage: uv run python -m app.jira "stripe autopay"')
        raise SystemExit(1)
    if not diagnose():
        raise SystemExit(1)
    with confluence._client() as client:
        r = client.post(f"{base_url()}/rest/api/3/search/jql",
                        json={"jql": build_jql(query, projects()), "maxResults": 10, "fields": SEARCH_FIELDS})
    print(f"Search: HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300])
        raise SystemExit(1)
    hits = [_summary_row(i, base_url()) for i in r.json().get("issues", []) if _allowed_key(i.get("key", ""), projects())]
    print(f"{len(hits)} ticket(s) found\n")
    for i, h in enumerate(hits, 1):
        print(f"{i}. {h['key']} [{h['status']}] {h['summary']}  (updated {h['last_updated'][:10]})")
        print(f"   {h['url']}")


if __name__ == "__main__":
    _cli()
