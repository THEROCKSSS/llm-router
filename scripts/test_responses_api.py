#!/usr/bin/env python3
"""Test the Codex wire path (/v1/responses + tools) across models."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("LLM_ROUTER_BASE", "http://127.0.0.1:4000/v1")
KEY_FILE = os.environ.get("LLM_ROUTER_KEY_FILE")
KEY_ENV = os.environ.get("LLM_ROUTER_KEY")

# EDIT this list to match YOUR router's model_list — anything can go here.
MODELS = [
    "upstream-a/fast",
    "ollama/deepseek-v4.1-flash",
    "ollama/gpt-oss:20b",
    "openrouter/deepseek/deepseek-v4.1-flash",
]

TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "Get the weather for a city",
    "parameters": {"type": "object",
                   "properties": {"city": {"type": "string"}},
                   "required": ["city"]},
}


def call(model: str, timeout: float = 150.0) -> dict:
    payload = {
        "model": model,
        "input": "What is the weather in Paris? Use the get_weather tool.",
        "tools": [TOOL],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/responses", data=body, method="POST")
    k = (Path(KEY_FILE).read_text(encoding="utf-8").strip() if KEY_FILE
         else (KEY_ENV or "").strip())
    if not k:
        raise SystemExit("Set LLM_ROUTER_KEY_FILE or LLM_ROUTER_KEY before running.")
    req.add_header("Authorization", "Bearer " + k)
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        dt = round((time.time() - t0) * 1000)
        out = d.get("output") or []
        kinds = [o.get("type") for o in out]
        fc = next((o for o in out if o.get("type") == "function_call"), None)
        if fc:
            return {"ok": True, "ms": dt, "kind": "function_call",
                    "detail": f"{fc.get('name')}({fc.get('arguments')})"}
        text = ""
        for o in out:
            if o.get("type") == "message":
                for c in o.get("content", []):
                    text += (c.get("text") or "")[:80]
        if d.get("error"):
            return {"ok": False, "ms": dt, "kind": "error",
                    "detail": json.dumps(d["error"])[:200]}
        return {"ok": bool(text), "ms": dt, "kind": "text-only",
                "detail": text[:100]}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"][:200]
        except Exception:
            detail = detail[:200]
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "kind": f"HTTP {e.code}", "detail": detail}
    except Exception as e:
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "kind": type(e).__name__, "detail": str(e)[:200]}


def main() -> int:
    results = []
    for m in MODELS:
        r = call(m)
        results.append({"model": m, **r})
        flag = "OK " if r["ok"] else "FAIL"
        print(f"[{flag}] {m:<42} {r['kind']:<14} {r['detail'][:70]}")
    out = Path(__file__).parent / "responses-api-results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    n = sum(1 for r in results if r["ok"])
    print(f"\n=== {n}/{len(results)} ok via /v1/responses (Codex wire) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
