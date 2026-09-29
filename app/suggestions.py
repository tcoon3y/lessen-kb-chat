"""Popular questions for the empty chat screen.

Kept in memory only (never logged or written to disk; resets on redeploy). A question is
suggested only after it has been asked at least twice and got a cited answer, so one-off
or personal questions never show up for others. Falls back to SUGGESTED_QUESTIONS.
"""
from __future__ import annotations

import re
import threading
import time

from app import config

MIN_COUNT = 2
WINDOW_SECONDS = 14 * 24 * 3600
MAX_TRACKED = 300
SHOW = 3

_lock = threading.Lock()
_seen: dict[str, dict] = {}  # "mode|normalized" -> {"text", "count", "last", "mode"}


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", " ".join(q.lower().split()))[:200]


def record(question: str, mode: str = "cs") -> None:
    q = " ".join(question.split())
    if not (8 <= len(q) <= 160):
        return
    key = f"{mode}|{_norm(q)}"
    now = time.time()
    with _lock:
        item = _seen.setdefault(key, {"text": q, "count": 0, "last": now, "mode": mode})
        item["count"] += 1
        item["last"] = now
        item["text"] = q
        if len(_seen) > MAX_TRACKED:  # drop the stalest
            for k in sorted(_seen, key=lambda k: _seen[k]["last"])[: len(_seen) - MAX_TRACKED]:
                del _seen[k]


DEFAULTS = {
    "cs": ["What can a vendor do on the free tier?", "How does the QBO integration work?",
           "What is the Free Early Pay offer?"],
    "product": ["What's the status of Stripe autopay?", "Which tickets are in the free tier epic?",
                "What's planned for the QBO integration?"],
    "feedback": ["What are the top open issues in the feedback tracker?",
                 "What did users say about scheduling in testing sessions?",
                 "Any support requests about invoices recently?"],
}


def defaults(mode: str = "cs") -> list[str]:
    raw = config.get({"product": "SUGGESTED_QUESTIONS_PRODUCT", "feedback": "SUGGESTED_QUESTIONS_FEEDBACK"}
                     .get(mode, "SUGGESTED_QUESTIONS"))
    items = [s.strip() for s in raw.split("|") if s.strip()] if raw else DEFAULTS.get(mode, DEFAULTS["cs"])
    return items[:SHOW]


def top(mode: str = "cs") -> list[str]:
    cutoff = time.time() - WINDOW_SECONDS
    with _lock:
        popular = sorted((v for v in _seen.values()
                          if v.get("mode", "cs") == mode and v["count"] >= MIN_COUNT and v["last"] >= cutoff),
                         key=lambda v: (v["count"], v["last"]), reverse=True)
        picked = [v["text"] for v in popular[:SHOW]]
    for d in defaults(mode):  # top up with defaults
        if len(picked) >= SHOW:
            break
        if _norm(d) not in {_norm(p) for p in picked}:
            picked.append(d)
    return picked


def reset() -> None:
    with _lock:
        _seen.clear()
