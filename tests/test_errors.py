import anthropic
import httpx
import httpx2

from app import errors

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def err(cls, status, msg):
    return cls(msg, response=httpx2.Response(status, request=REQ), body=None)


def test_claude_errors():
    assert errors.classify(err(anthropic.BadRequestError, 400, "Your credit balance is too low"))["kind"] == "claude_limit"
    assert errors.classify(err(anthropic.RateLimitError, 429, "rate limited"))["kind"] == "claude_rate_limit"
    assert errors.classify(err(anthropic.InternalServerError, 529, "overloaded"))["kind"] == "claude_down"
    assert errors.classify(err(anthropic.AuthenticationError, 401, "bad key"))["kind"] == "claude_auth"
    assert errors.classify(RuntimeError("x"))["kind"] == "unknown"


def test_confluence_mapping():
    req = httpx.Request("GET", "https://x/wiki/rest/api/search")
    e401 = httpx.HTTPStatusError("x", request=req, response=httpx.Response(401, request=req))
    e429 = httpx.HTTPStatusError("x", request=req, response=httpx.Response(429, request=req))
    assert isinstance(errors.from_confluence(e401), errors.ConfluenceLoginError)
    assert errors.classify(errors.from_confluence(e429))["kind"] == "confluence_rate_limit"
    assert errors.classify(errors.from_confluence(httpx.ConnectError("down")))["kind"] == "confluence_down"
    e404 = httpx.HTTPStatusError("x", request=req, response=httpx.Response(404, request=req))
    assert errors.from_confluence(e404) is None
