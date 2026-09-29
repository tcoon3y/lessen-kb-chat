"""FastAPI entry point for Lessen Pro KB Chat."""
from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from typing import Literal

from pydantic import BaseModel, Field

from app import agent, config, docs_requests, errors, jira, suggestions, usage

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("kbchat")
app = FastAPI(title="Lessen Pro KB Chat", docs_url=None, redoc_url=None, openapi_url=None)


class Turn(BaseModel):
    role: str
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["cs", "product"] = "cs"
    history: list[Turn] = Field(default_factory=list, max_length=40)


class DocsRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    subject: str = Field(default="", max_length=80)


class Feedback(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    note: str = Field(default="", max_length=500)
    sources: list[str] = Field(default_factory=list, max_length=10)


def check_passcode(x_passcode: str | None) -> None:
    """AUTH_MODE=passcode: require the X-Passcode header. Other modes rely on Cloudflare Access."""
    if config.get("AUTH_MODE", "passcode").lower() != "passcode":
        return
    expected = config.get("APP_PASSCODE")
    if not expected:
        raise HTTPException(503, "Passcode not configured.")
    if not x_passcode or not hmac.compare_digest(x_passcode.encode(), expected.encode()):
        raise HTTPException(401, "Wrong passcode.")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def ui_config() -> dict:
    return {
        "passcode_required": config.get("AUTH_MODE", "passcode").lower() == "passcode",
        "docs_requests_enabled": bool(config.get("DOCS_REQUEST_PAGE_ID").strip()),
        "jira_projects": config.get("JIRA_PROJECTS", "LP"),
        "docs_requests_url": (f"{config.get('CONFLUENCE_BASE_URL').rstrip('/')}/pages/viewpage.action?pageId="
                              f"{config.get('DOCS_REQUEST_PAGE_ID').strip()}"
                              if config.get("DOCS_REQUEST_PAGE_ID").strip() else ""),
    }


@app.get("/api/suggestions")
def get_suggestions(mode: Literal["cs", "product"] = "cs", x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    return {"questions": suggestions.top(mode)}


@app.get("/api/usage")
def get_usage(x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    return usage.summary()


@app.get("/api/doc-stats")
def doc_stats(x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    if not config.get("DOCS_REQUEST_PAGE_ID").strip():
        raise HTTPException(404, "Doc requests aren't set up.")
    try:
        return docs_requests.stats()
    except Exception as exc:
        log.error("doc stats failed: %s", type(exc).__name__)
        raise HTTPException(502, "Couldn't read the Documentation Requests page.")


@app.post("/api/login")
def login(x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    return {"ok": True}


def _who(request: Request, cf_email: str | None) -> str:
    """Rate-limit key: the Cloudflare Access email if present, else the client IP."""
    return (cf_email or (request.client.host if request.client else "unknown")).lower()


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, x_passcode: str | None = Header(default=None),
         cf_access_authenticated_user_email: str | None = Header(default=None)) -> StreamingResponse:
    check_passcode(x_passcode)
    history = [t.model_dump() for t in req.history]
    if not usage.allow(_who(request, cf_access_authenticated_user_email)):
        usage.record_error("rate_limited")
        log.info('{"event": "chat", "outcome": "rate_limited"}')
        err = {"type": "error", "kind": "rate_limited", "title": "Slow down a little",
               "text": f"You've asked {usage.RATE_LIMIT} questions in the last {usage.RATE_WINDOW // 60} minutes. "
                       "Please wait a few minutes and try again."}
        return StreamingResponse(iter([_sse(err)]), media_type="text/event-stream")

    def events():
        try:
            for event in agent.stream_answer(req.question, history, check_login=True, mode=req.mode):
                if event["type"] == "done":
                    nd = bool(event.get("not_documented"))
                    if event.get("sources") and not nd:
                        suggestions.record(req.question, req.mode)
                    meta = event.get("meta", {})
                    usage.record_answer(meta, nd)
                    # One line per request: numbers only, never question or answer text.
                    log.info(json.dumps({"event": "chat", "mode": req.mode, "outcome": "not_documented" if nd else "answered",
                                         "latency_ms": meta.get("latency_ms"), "tool_calls": meta.get("tool_calls"),
                                         "tokens": meta.get("tokens"), "sources": len(event.get("sources") or []),
                                         "history_turns": len(history)}))
                yield _sse(event)
        except Exception as exc:  # never leak details or content
            err = errors.classify(exc)
            usage.record_error(err["kind"])
            log.error(json.dumps({"event": "chat", "outcome": "error", "kind": err["kind"],
                                  "exception": type(exc).__name__}))
            yield _sse({"type": "error", **err})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/request-docs")
def request_docs(req: DocsRequest, x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    try:
        subject = req.subject.strip() or agent.make_subject(req.question)
        kind = "Request" if req.subject.strip() else "Not documented"
        docs_requests.add_request(subject, req.question, kind=kind)
        usage.record_event("doc_requests")
    except docs_requests.RequestsPageError as exc:
        log.error("docs request failed: %s", exc)  # status + Confluence message only
        raise HTTPException(502, "Couldn't save the request. Please try again.")
    except Exception as exc:
        log.error("docs request failed: %s", type(exc).__name__)
        raise HTTPException(502, "Couldn't save the request. Please try again.")
    return {"ok": True}


@app.post("/api/feedback")
def feedback(req: Feedback, x_passcode: str | None = Header(default=None)) -> dict:
    """'Report incorrect answer': adds a row to the same Doc requests table."""
    check_passcode(x_passcode)
    detail = req.question.strip()
    if req.note.strip():
        detail += f" | Feedback: {req.note.strip()}"
    if req.sources:
        detail += " | Cited: " + "; ".join(s[:120] for s in req.sources)
    try:
        docs_requests.add_request(agent.make_subject(req.question), detail, kind="Incorrect answer")
        usage.record_event("feedback")
    except docs_requests.RequestsPageError as exc:
        log.error("feedback failed: %s", exc)
        raise HTTPException(502, "Couldn't save the feedback. Please try again.")
    except Exception as exc:
        log.error("feedback failed: %s", type(exc).__name__)
        raise HTTPException(502, "Couldn't save the feedback. Please try again.")
    return {"ok": True}
