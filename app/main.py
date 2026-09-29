"""FastAPI entry point for Lessen Pro KB Chat."""
from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import agent, config, docs_requests

STATIC_DIR = Path(__file__).parent / "static"
FRIENDLY_ERROR = "Sorry, something went wrong answering that. Please try again in a moment."

log = logging.getLogger("kbchat")
app = FastAPI(title="Lessen Pro KB Chat", docs_url=None, redoc_url=None, openapi_url=None)


class Turn(BaseModel):
    role: str
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=40)


class DocsRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


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
    }


@app.post("/api/login")
def login(x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    return {"ok": True}


@app.post("/api/chat")
def chat(req: ChatRequest, x_passcode: str | None = Header(default=None)) -> StreamingResponse:
    check_passcode(x_passcode)
    history = [t.model_dump() for t in req.history]

    def events():
        try:
            for event in agent.stream_answer(req.question, history):
                yield _sse(event)
        except Exception as exc:  # never leak details or content
            log.error("chat failed: %s", type(exc).__name__)
            yield _sse({"type": "error", "text": FRIENDLY_ERROR})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/request-docs")
def request_docs(req: DocsRequest, x_passcode: str | None = Header(default=None)) -> dict:
    check_passcode(x_passcode)
    try:
        docs_requests.add_request(agent.make_subject(req.question), req.question)
    except Exception as exc:
        log.error("docs request failed: %s", type(exc).__name__)
        raise HTTPException(502, "Couldn't save the request. Please try again.")
    return {"ok": True}
