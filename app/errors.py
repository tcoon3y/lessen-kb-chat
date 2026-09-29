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


def _msg(exc: Exception) -> str:
    return str(getattr(exc, "message", "") or exc).lower()


def classify(exc: Exception) -> dict:
    """Return {"kind", "title", "text"} for the chat page, plus a log-safe kind."""
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
