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
_seen: dict[str, dict] = {}  # normalized -> {"text", "count", "last"}


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", " ".join(q.lower().split()))[:200]


def record(question: str) -> None:
    q = " ".join(question.split())
    if not (8 <= len(q) <= 160):
        return
    key = _norm(q)
    now = time.time()
    with _lock:
        item = _seen.setdefault(key, {"text": q, "count": 0, "last": now})
        item["count"] += 1
        item["last"] = now
        item["text"] = q
        if len(_seen) > MAX_TRACKED:  # drop the stalest
            for k in sorted(_seen, key=lambda k: _seen[k]["last"])[: len(_seen) - MAX_TRACKED]:
                del _seen[k]


def defaults() -> list[str]:
    raw = config.get("SUGGESTED_QUESTIONS")
    items = [s.strip() for s in raw.split("|") if s.strip()] if raw else [
        "What can a vendor do on the free tier?",
        "How does the QBO integration work?",
        "What is the Free Early Pay offer?",
    ]
    return items[:SHOW]


def top() -> list[str]:
    cutoff = time.time() - WINDOW_SECONDS
    with _lock:
        popular = sorted((v for v in _seen.values() if v["count"] >= MIN_COUNT and v["last"] >= cutoff),
                         key=lambda v: (v["count"], v["last"]), reverse=True)
        picked = [v["text"] for v in popular[:SHOW]]
    for d in defaults():  # top up with defaults
        if len(picked) >= SHOW:
            break
        if _norm(d) not in {_norm(p) for p in picked}:
            picked.append(d)
    return picked


def reset() -> None:
    with _lock:
        _seen.clear()
