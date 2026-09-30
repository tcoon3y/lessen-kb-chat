"""In-memory usage stats and per-person rate limit. Counts only: never question or answer text.

Stats reset when the app restarts or redeploys. Costs are estimates from token counts.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone

from app import config, db

RATE_LIMIT = 20            # questions
RATE_WINDOW = 10 * 60      # seconds
_CHICAGO = timezone(timedelta(hours=-5))

_lock = threading.Lock()
_started = datetime.now(timezone.utc)
_days: dict[str, dict] = defaultdict(lambda: {
    "questions": 0, "answered": 0, "not_documented": 0, "errors": 0,
    "latency_ms": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
    "doc_requests": 0, "feedback": 0, "confluence_calls": 0, "confluence_429": 0,
})
_quota: dict = {}  # latest rate-limit headers Atlassian sent, if any
_error_kinds: dict[str, int] = defaultdict(int)
_hits: dict[str, deque] = defaultdict(deque)


def _today() -> str:
    return datetime.now(_CHICAGO).date().isoformat()


def _price(name: str, default: float) -> float:
    try:
        return float(config.get(name) or default)
    except ValueError:
        return default


def cost(tokens: dict) -> float:
    """Estimated USD. Override prices (per million tokens) with PRICE_INPUT_PER_MTOK / PRICE_OUTPUT_PER_MTOK."""
    pin, pout = _price("PRICE_INPUT_PER_MTOK", 3.0), _price("PRICE_OUTPUT_PER_MTOK", 15.0)
    return (tokens.get("input", 0) * pin + tokens.get("output", 0) * pout
            + tokens.get("cache_read", 0) * pin * 0.1 + tokens.get("cache_write", 0) * pin * 1.25) / 1e6


def allow(who: str, now: float | None = None) -> bool:
    """Sliding-window limit: RATE_LIMIT questions per RATE_WINDOW per person."""
    now = now or time.monotonic()
    with _lock:
        q = _hits[who]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True


def record_answer(meta: dict, not_documented: bool) -> None:
    t = meta.get("tokens", {})
    with _lock:
        d = _days[_today()]
        d["questions"] += 1
        d["answered"] += 0 if not_documented else 1
        d["not_documented"] += 1 if not_documented else 0
        d["latency_ms"] += int(meta.get("latency_ms", 0))
        for k in ("input", "output", "cache_read", "cache_write"):
            d[k] += int(t.get(k, 0))


def record_error(kind: str) -> None:
    with _lock:
        d = _days[_today()]
        d["questions"] += 1
        d["errors"] += 1
        _error_kinds[kind] += 1


QUOTA_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                 "x-ratelimit-nearlimit", "ratelimit-reason", "retry-after")


def record_confluence(status: int, headers, service: str = "atlassian") -> None:
    """Called for every Confluence API response (via an httpx hook). Numbers and header values only."""
    db.bump_api_call(service, status == 429)
    with _lock:
        d = _days[_today()]
        d["confluence_calls"] += 1
        if status == 429:
            d["confluence_429"] += 1
        seen = {h: headers.get(h) for h in QUOTA_HEADERS if headers.get(h)}
        if seen:
            lowest = _quota.get("lowest_remaining")
            _quota.update(seen)
            _quota["at"] = datetime.now(timezone.utc).isoformat()
            try:
                rem = int(seen["x-ratelimit-remaining"])
                _quota["lowest_remaining"] = rem if lowest is None else min(lowest, rem)
            except (KeyError, ValueError):
                if lowest is not None:
                    _quota["lowest_remaining"] = lowest


def record_event(name: str) -> None:  # "doc_requests" or "feedback"
    with _lock:
        _days[_today()][name] += 1


def _day_cost(d: dict) -> float:
    return cost({k: d.get(k, 0) for k in ("input", "output", "cache_read", "cache_write")})


def summary(days: int = 14) -> dict:
    now = datetime.now(_CHICAGO)
    today = now.date()
    month_start = datetime(today.year, today.month, 1, tzinfo=_CHICAGO)
    next_month = datetime(today.year + (today.month == 12), today.month % 12 + 1, 1, tzinfo=_CHICAGO)
    days_in_month = (next_month - month_start).days
    # projection covers the whole month: what's spent so far + run-rate for the remaining time
    with _lock:
        month = {k: dict(v) for k, v in _days.items() if k[:7] == today.isoformat()[:7]}
        series = []
        for i in range(days - 1, -1, -1):
            key = (today - timedelta(days=i)).isoformat()
            d = _days.get(key, {})
            series.append({"date": key, "cost": round(_day_cost(d), 4), "questions": d.get("questions", 0)})
        quota = dict(_quota)
        errors = dict(_error_kinds)

    cost_month = sum(_day_cost(d) for d in month.values())
    calls_month = sum(d.get("confluence_calls", 0) for d in month.values())
    start = max(month_start, _started.astimezone(_CHICAGO))
    stored = db.ready()
    if stored:  # the database has the whole month, not just since the last restart
        rows = db.daily_usage((today - timedelta(days=max(days, today.day) - 1)).isoformat())
        by_day = {r["date"]: r for r in rows}
        series = [{"date": s["date"], "cost": round(by_day.get(s["date"], {}).get("cost", 0.0), 4),
                   "questions": by_day.get(s["date"], {}).get("questions", 0)} for s in series]
        mrows = [r for r in rows if r["date"][:7] == today.isoformat()[:7]]
        cost_month = sum(r["cost"] for r in mrows)
        calls = [c for c in db.api_calls(month_start.date().isoformat())]
        calls_month = sum(c["calls"] for c in calls)
        month = {r["date"]: {"questions": r["questions"], "confluence_calls": 0, "confluence_429": 0,
                             **{k: r[k] for k in ("input", "output", "cache_read", "cache_write")}} for r in mrows}
        for c in calls:
            month.setdefault(c["date"], {"questions": 0})
            month[c["date"]]["confluence_calls"] = month[c["date"]].get("confluence_calls", 0) + c["calls"]
            month[c["date"]]["confluence_429"] = month[c["date"]].get("confluence_429", 0) + c["rate_limited"]
        errors = db.error_kinds(month_start.date().isoformat()) or errors
        first = db.first_seen()
        if first:
            start = max(month_start, datetime.fromisoformat(first).astimezone(_CHICAGO))
    elapsed = max((now - start).total_seconds() / 86400, 0)
    remaining = max((next_month - now).total_seconds() / 86400, 0)
    enough = elapsed >= 1 / 24

    def project(total: float) -> float | None:
        return round(total + total / elapsed * remaining, 2) if enough and elapsed else None

    try:
        budget = float(config.get("ANTHROPIC_MONTHLY_BUDGET") or 0) or None
    except ValueError:
        budget = None
    proj_cost = project(cost_month)
    today_d = month.get(today.isoformat(), {})
    if stored:
        today_row = next((r for r in db.daily_usage(today.isoformat())), None)
        cost_today = today_row["cost"] if today_row else 0.0
    else:
        cost_today = _day_cost(today_d)
    return {
        "since": _started.isoformat(),
        "stored": stored,
        "days_of_data": round(elapsed, 1),
        "claude": {
            "cost_today": round(cost_today, 2),
            "cost_month": round(cost_month, 2),
            "projected_month": proj_cost,
            "budget": budget,
            "budget_used_pct": round(100 * cost_month / budget) if budget else None,
            "projected_pct": round(100 * proj_cost / budget) if budget and proj_cost is not None else None,
            "questions_month": sum(d.get("questions", 0) for d in month.values()),
            "series": series,
        },
        "confluence": {
            "calls_today": today_d.get("confluence_calls", 0),
            "calls_month": calls_month,
            "projected_month": round(project(calls_month)) if project(calls_month) is not None else None,
            "rate_limited_month": sum(d.get("confluence_429", 0) for d in month.values()),
            "quota": quota or None,
        },
        "error_kinds": errors,
        "rate_limit": f"{RATE_LIMIT} questions per {RATE_WINDOW // 60} minutes per person",
    }


def reset() -> None:
    with _lock:
        _days.clear(); _error_kinds.clear(); _hits.clear(); _quota.clear()
