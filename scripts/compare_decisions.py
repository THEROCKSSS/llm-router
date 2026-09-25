#!/usr/bin/env python3
"""Side-by-side decision comparison: run one state through TWO decision lanes.

Rendered identically on purpose — any difference you see is the engines, not
the plumbing. Works with any two lanes on a router (Jev and Laya are the
canonical pair; see docs/03-JEV-AND-LAYA.md).

USAGE
-----
    python compare_decisions.py
    python compare_decisions.py --state "Should we migrate this weekend?"
    python compare_decisions.py --state "..." --questions-json questions.json
    python compare_decisions.py --lane-a laya --lane-b myjev \
        --base-url http://127.0.0.1:4000/v1 --key-file ~/.secrets/router-key

--questions-json: a file with the question dict; it gets wrapped together with
the state as {"state": ..., "questions": {...}} in the message, which both the
Jev and Laya adapters accept verbatim.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_STATE = (
    "Subject: Duplicate charge on invoice #4411\n"
    "Hi, we were billed twice for March. Please refund the duplicate today "
    "or we will cancel our plan."
)


def read_key(key_file: str | None) -> str:
    if key_file:
        return Path(key_file).expanduser().read_text(encoding="utf-8").strip()
    import os
    key = os.environ.get("LLM_ROUTER_KEY", "").strip()
    if not key:
        sys.exit("No key: pass --key-file or set LLM_ROUTER_KEY")
    return key


def ask(base_url: str, model: str, content: str, api_key: str, timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 700,
    }).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=body, method="POST")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        return {"ok": True, "text": d["choices"][0]["message"].get("content") or "",
                "ms": round((time.time() - t0) * 1000)}
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
        return {"ok": False, "text": f"{type(e).__name__}: {e}\n{detail}",
                "ms": round((time.time() - t0) * 1000)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--questions-json", default=None,
                    help="JSON file with a questions dict")
    ap.add_argument("--lane-a", default="laya", help="first lane/model name")
    ap.add_argument("--lane-b", default="laya", help="second lane/model name")
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    ap.add_argument("--base-url", default="http://127.0.0.1:4000/v1")
    ap.add_argument("--key-file", default=None)
    ap.add_argument("--timeout", type=float, default=240.0)
    args = ap.parse_args()

    content = args.state
    if args.questions_json:
        q = json.loads(Path(args.questions_json).read_text(encoding="utf-8"))
        content = json.dumps({"state": args.state, "questions": q})

    api_key = read_key(args.key_file)

    print("=" * 70)
    print(f"STATE: {args.state[:160]}{'...' if len(args.state) > 160 else ''}")
    print("=" * 70)

    for label, model in ((args.label_a or f"lane A [{args.lane_a}]", args.lane_a),
                         (args.label_b or f"lane B [{args.lane_b}]", args.lane_b)):
        out = ask(args.base_url, model, content, api_key, args.timeout)
        print(f"\n--- {label}  {out['ms']} ms ---")
        print(out["text"] if out["ok"] else f"FAILED: {out['text'][:300]}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
