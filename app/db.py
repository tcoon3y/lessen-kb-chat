"""Optional Postgres storage (Railway Postgres via DATABASE_URL).

Stores chats (question, answer, sources, timing, tokens, cost), feedback/doc-request events,
and daily API call counts. Question/answer text is cleared after RETENTION_DAYS; counts stay.
Every function is best-effort: if the database is missing or down, the bot keeps working.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from app import config

log = logging.getLogger("kbchat")
_ready = False
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    day           DATE NOT NULL DEFAULT (now() AT TIME ZONE 'America/Chicago')::date,
    mode          TEXT NOT NULL,
    session_id    TEXT,
    question      TEXT,
    question_norm TEXT,
    answer        TEXT,
    sources       JSONB,
    outcome       TEXT NOT NULL,          -- answered | not_documented | error | rate_limited
    error_kind    TEXT,
    latency_ms    INTEGER,
    tool_calls    JSONB,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read    INTEGER DEFAULT 0,
    cache_write   INTEGER DEFAULT 0,
    cost_usd      NUMERIC(10, 5) DEFAULT 0,
    history_turns INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chats_day_idx ON chats (day);
CREATE INDEX IF NOT EXISTS chats_mode_norm_idx ON chats (mode, question_norm);

CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    day        DATE NOT NULL DEFAULT (now() AT TIME ZONE 'America/Chicago')::date,
    kind       TEXT NOT NULL,             -- doc_request | not_documented_request | incorrect_answer
    mode       TEXT,
    subject    TEXT,
    question   TEXT,
    note       TEXT,
    sources    JSONB
);
CREATE INDEX IF NOT EXISTS events_day_idx ON events (day);

CREATE TABLE IF NOT EXISTS api_calls (
    day          DATE NOT NULL,
    service      TEXT NOT NULL,           -- atlassian | notion
    calls        INTEGER NOT NULL DEFAULT 0,
    rate_limited INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, service)
);
"""


def enabled() -> bool:
    return bool(config.get("DATABASE_URL").strip())


def retention_days() -> int:
    try:
        return int(config.get("RETENTION_DAYS") or 365)
    except ValueError:
        return 365


def _connect():
    import psycopg
    return psycopg.connect(config.get("DATABASE_URL").strip(), connect_timeout=5, autocommit=True)


def init() -> bool:
    """Create tables if needed. Returns True when the database is usable."""
    global _ready
    if not enabled():
        return False
    try:
        with _connect() as conn:
            conn.execute(SCHEMA)
        _ready = True
        cleanup()
    except Exception as exc:
        _ready = False
        log.error("database unavailable: %s", type(exc).__name__)
    return _ready


def ready() -> bool:
    return enabled() and _ready


def _run(sql: str, params: tuple = (), fetch: bool = False):
    if not ready():
        return None
    try:
        with _connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if fetch else None
    except Exception as exc:
        log.error("database write/read failed: %s", type(exc).__name__)
        return None


def norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", " ".join((q or "").lower().split()))[:200]


def log_chat(mode: str, question: str, answer: str | None, sources: list | None, outcome: str,
             meta: dict | None = None, error_kind: str | None = None, cost: float = 0.0,
             session_id: str | None = None, history_turns: int = 0) -> None:
    meta = meta or {}
    t = meta.get("tokens") or {}
    _run("""INSERT INTO chats (mode, session_id, question, question_norm, answer, sources, outcome, error_kind,
                latency_ms, tool_calls, input_tokens, output_tokens, cache_read, cache_write, cost_usd, history_turns)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
         (mode, session_id, question, norm(question), answer, json.dumps(sources or []), outcome, error_kind,
          meta.get("latency_ms"), json.dumps(meta.get("tool_calls") or []), int(t.get("input", 0)),
          int(t.get("output", 0)), int(t.get("cache_read", 0)), int(t.get("cache_write", 0)), round(cost, 5),
          history_turns))


def log_event(kind: str, question: str, subject: str = "", note: str = "", mode: str | None = None,
              sources: list | None = None) -> None:
    _run("INSERT INTO events (kind, mode, subject, question, note, sources) VALUES (%s,%s,%s,%s,%s,%s)",
         (kind, mode, subject or None, question, note or None, json.dumps(sources or [])))


def bump_api_call(service: str, rate_limited: bool) -> None:
    _run("""INSERT INTO api_calls (day, service, calls, rate_limited)
            VALUES ((now() AT TIME ZONE 'America/Chicago')::date, %s, 1, %s)
            ON CONFLICT (day, service) DO UPDATE SET calls = api_calls.calls + 1,
                rate_limited = api_calls.rate_limited + EXCLUDED.rate_limited""",
         (service, 1 if rate_limited else 0))


def cleanup() -> None:
    """Clear question/answer text older than the retention period. Counts and costs are kept."""
    days = retention_days()
    _run("""UPDATE chats SET question = NULL, question_norm = NULL, answer = NULL, sources = NULL
            WHERE created_at < now() - make_interval(days => %s) AND question IS NOT NULL""", (days,))
    _run("""UPDATE events SET question = NULL, note = NULL, subject = NULL
            WHERE created_at < now() - make_interval(days => %s) AND question IS NOT NULL""", (days,))


def start_maintenance() -> None:
    """Create tables, then clear expired text once a day in the background."""
    def loop():
        init()
        while True:
            time.sleep(24 * 3600)
            if ready():
                cleanup()
    if enabled():
        threading.Thread(target=loop, daemon=True).start()


# ---------- reads for the Usage page and suggestions ----------

def daily_usage(since_day: str) -> list[dict]:
    rows = _run("""SELECT day::text, count(*) AS questions,
                          sum(input_tokens), sum(output_tokens), sum(cache_read), sum(cache_write),
                          coalesce(sum(cost_usd), 0)::float, count(*) FILTER (WHERE outcome = 'not_documented'),
                          count(*) FILTER (WHERE outcome IN ('error', 'rate_limited'))
                   FROM chats WHERE day >= %s::date GROUP BY day ORDER BY day""", (since_day,), fetch=True)
    return [{"date": r[0], "questions": r[1], "input": r[2] or 0, "output": r[3] or 0, "cache_read": r[4] or 0,
             "cache_write": r[5] or 0, "cost": r[6], "not_documented": r[7], "errors": r[8]} for r in rows or []]


def api_calls(since_day: str) -> list[dict]:
    rows = _run("SELECT day::text, service, calls, rate_limited FROM api_calls WHERE day >= %s::date",
                (since_day,), fetch=True)
    return [{"date": r[0], "service": r[1], "calls": r[2], "rate_limited": r[3]} for r in rows or []]


def error_kinds(since_day: str) -> dict:
    rows = _run("""SELECT error_kind, count(*) FROM chats WHERE day >= %s::date AND error_kind IS NOT NULL
                   GROUP BY error_kind""", (since_day,), fetch=True)
    return {r[0]: r[1] for r in rows or []}


def first_seen() -> str | None:
    rows = _run("SELECT min(created_at)::text FROM chats", fetch=True)
    return rows[0][0] if rows and rows[0][0] else None


def popular(mode: str, min_count: int = 2, days: int = 14, limit: int = 3) -> list[str] | None:
    rows = _run("""SELECT (array_agg(question ORDER BY created_at DESC))[1], count(*) AS n, max(created_at) AS last
                   FROM chats
                   WHERE mode = %s AND outcome = 'answered' AND question_norm IS NOT NULL
                     AND jsonb_array_length(coalesce(sources, '[]'::jsonb)) > 0
                     AND length(question) BETWEEN 8 AND 160
                     AND created_at > now() - make_interval(days => %s)
                   GROUP BY question_norm HAVING count(*) >= %s
                   ORDER BY n DESC, last DESC LIMIT %s""", (mode, days, min_count, limit), fetch=True)
    return None if rows is None else [r[0] for r in rows]
