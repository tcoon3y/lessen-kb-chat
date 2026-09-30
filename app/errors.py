"""Turn failures into clear, user-facing error states. Never includes secrets or content."""
from __future__ import annotations

import anthropic
import httpx


class ConfluenceLoginError(Exception):
    """Confluence rejected the email/token (or treats us as logged out)."""


class ConfluenceUnavailable(Exception):
    """Confluence is rate-limiting us or can't be reached."""

    def __init__(self, status: int | None = None):
        super().__init__(f"Confluence unavailable ({status})")
        self.status = status


class NotionLoginError(Exception):
    """Notion rejected the token, or the research page isn't shared with the integration."""


class SourceFailure(Exception):
    """A source (Confluence, Jira, Notion, feedback sheet) failed, so the bot couldn't check it.

    Raised at the end of an answer instead of letting Claude say "not documented"."""

    def __init__(self, failures: list[dict]):
        super().__init__("source failure: " + ", ".join(f"{f['source']}:{f['kind']}" for f in failures))
        self.failures = failures


SOURCE_NAMES = {"confluence": "Confluence", "jira": "Jira", "notion": "Notion (user research)",
                "sheet": "the feedback tracker"}
REASONS = {
    "login": "its login was rejected (the token may have expired)",
    "no_access": "the bot doesn't have permission",
    "rate_limit": "it's limiting requests right now",
    "down": "it isn't responding",
    "loading": "it's still loading after a restart (takes a few minutes)",
    "not_connected": "it isn't connected yet",
    "error": "it returned an error",
}
TRANSIENT = ("rate_limit", "down")


def source_kind(exc: Exception) -> str:
    """Map a failure while calling a source to a short kind (see REASONS)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return "login"
        if code == 403:
            return "no_access"
        if code == 429:
            return "rate_limit"
        if code >= 500:
            return "down"
        return "error"
    if isinstance(exc, httpx.TransportError):
        return "down"
    if isinstance(exc, (PermissionError, NotionLoginError, ConfluenceLoginError)):
        return "login"
    if isinstance(exc, ConfluenceUnavailable):
        return "rate_limit" if exc.status == 429 else "down"
    return "error"


def failure_text(failures: list[dict]) -> str:
    return "; ".join(f"{SOURCE_NAMES.get(f['source'], f['source'])}: {REASONS.get(f['kind'], REASONS['error'])}"
                     for f in failures)


def _msg(exc: Exception) -> str:
    return str(getattr(exc, "message", "") or exc).lower()


def classify(exc: Exception) -> dict:
    """Return {"kind", "title", "text"} for the chat page, plus a log-safe kind."""
    if isinstance(exc, SourceFailure) and exc.failures:
        f0 = exc.failures[0]
        names = [SOURCE_NAMES.get(f["source"], f["source"]) for f in exc.failures]
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        only_waiting = all(f["kind"] in ("loading", "not_connected") for f in exc.failures)
        owner = any(f["kind"] in ("login", "no_access") for f in exc.failures)
        return {"kind": f"{f0['source']}_{f0['kind']}", "title": f"Couldn't check {who}",
                "text": f"{failure_text(exc.failures)[0].upper()}{failure_text(exc.failures)[1:]}. "
                        "So this isn't a sign the answer is undocumented; the bot just couldn't look. "
                        + ("Please let the bot owner know." if owner else
                           "Try again in a few minutes." if only_waiting else "Please try again in a minute.")}
    if isinstance(exc, ConfluenceLoginError):
        return {"kind": "confluence_login", "title": "Can't connect to Confluence",
                "text": "The bot's Confluence access isn't working, so it can't look anything up. "
                        "Its Atlassian token may have expired. Please let the bot owner know."}
    if isinstance(exc, NotionLoginError):
        return {"kind": "notion_login", "title": "Can't connect to Notion",
                "text": "The bot's Notion access isn't working, so it can't read user research. "
                        "Please let the bot owner know."}
    if isinstance(exc, ConfluenceUnavailable):
        if exc.status == 429:
            return {"kind": "confluence_rate_limit", "title": "Confluence is busy",
                    "text": "Confluence is limiting requests right now. Please try again in a minute."}
        return {"kind": "confluence_down", "title": "Confluence isn't responding",
                "text": "The bot couldn't reach Confluence. Please try again in a minute."}
    if isinstance(exc, anthropic.AuthenticationError):
        return {"kind": "claude_auth", "title": "Claude key isn't working",
                "text": "The bot's Claude API key was rejected. Please let the bot owner know."}
    if isinstance(exc, (anthropic.BadRequestError, anthropic.PermissionDeniedError)) and any(
            w in _msg(exc) for w in ("credit", "billing", "spend", "usage limit", "limit reached")):
        return {"kind": "claude_limit", "title": "Monthly usage limit reached",
                "text": "The bot has used up its Claude budget for now, so it can't answer questions. "
                        "Please let the bot owner know."}
    if isinstance(exc, anthropic.RateLimitError):
        return {"kind": "claude_rate_limit", "title": "Too many questions at once",
                "text": "Claude is limiting requests right now. Please try again in a minute."}
    if isinstance(exc, (anthropic.InternalServerError, anthropic.APIConnectionError)) or \
            getattr(exc, "status_code", None) in (500, 502, 503, 529):
        return {"kind": "claude_down", "title": "Claude is busy",
                "text": "Claude is overloaded or unreachable right now. Please try again in a minute."}
    return {"kind": "unknown", "title": "Something went wrong",
            "text": "The bot couldn't answer that. Please try again in a moment."}


def from_confluence(exc: Exception) -> Exception | None:
    """Map an httpx failure during a Confluence call to one of our errors, or None to keep going."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return ConfluenceLoginError()
        if code in (429, 502, 503, 504):
            return ConfluenceUnavailable(code)
    if isinstance(exc, httpx.TransportError):
        return ConfluenceUnavailable(None)
    return None
