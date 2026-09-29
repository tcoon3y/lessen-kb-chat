"""Run the eval question set against live Confluence + Claude and report which pages were cited.

Usage: uv run python -m tests.run_eval
"""
from pathlib import Path

import yaml

from app import agent

QUESTIONS = Path(__file__).parent / "eval_questions.yaml"


def main() -> None:
    items = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    passed = 0
    for i, item in enumerate(items, 1):
        q, expect = item["question"], str(item.get("expect", "")).strip()
        result = agent.answer(q)
        titles = [s["title"] for s in result["sources"]]
        not_found = agent.is_not_documented(result["text"])
        if expect.upper() == "NONE":
            ok = not_found
        else:
            ok = any(expect.lower() in t.lower() for t in titles)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {i}. {q}")
        print(f"      expected: {expect}")
        print(f"      cited:    {'; '.join(titles) or ('(not documented reply)' if not_found else '(none)')}")
        m = result["meta"]
        print(f"      {m['latency_ms'] / 1000:.1f}s, {len(m['tool_calls'])} tool calls\n")
    print(f"Score: {passed}/{len(items)}  (target: at least 8/10)")


if __name__ == "__main__":
    main()
