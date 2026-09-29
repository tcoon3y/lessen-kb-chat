from app import usage


def test_sliding_window():
    usage.reset()
    for i in range(usage.RATE_LIMIT):
        assert usage.allow("a", now=1000 + i)
    assert not usage.allow("a", now=1000 + usage.RATE_LIMIT)
    assert usage.allow("b", now=1000)                       # other people unaffected
    assert usage.allow("a", now=1000 + usage.RATE_WINDOW + 1)  # window slides


def test_cost_estimate(monkeypatch):
    monkeypatch.setenv("PRICE_INPUT_PER_MTOK", "3")
    monkeypatch.setenv("PRICE_OUTPUT_PER_MTOK", "15")
    assert round(usage.cost({"input": 1_000_000, "output": 100_000}), 2) == 4.5


def test_confluence_calls_and_quota_headers():
    usage.reset()
    usage.record_confluence(200, {"x-ratelimit-remaining": "480", "x-ratelimit-limit": "500"})
    usage.record_confluence(429, {"retry-after": "30"})
    c = usage.summary()["confluence"]
    assert c["calls_today"] == 2 and c["rate_limited_month"] == 1
    assert c["quota"]["retry-after"] == "30"


def test_budget(monkeypatch):
    usage.reset()
    monkeypatch.setenv("ANTHROPIC_MONTHLY_BUDGET", "50")
    usage.record_answer({"latency_ms": 1, "tokens": {"input": 1_000_000, "output": 0}}, False)
    c = usage.summary()["claude"]
    assert c["cost_month"] == 3.0 and c["budget"] == 50 and c["budget_used_pct"] == 6


def test_quota_tracks_lowest_remaining():
    usage.reset()
    for rem in ("399", "372", "399"):
        usage.record_confluence(200, {"x-ratelimit-remaining": rem, "x-ratelimit-limit": "400"})
    q = usage.summary()["confluence"]["quota"]
    assert q["x-ratelimit-remaining"] == "399" and q["lowest_remaining"] == 372
