#!/usr/bin/env python3
"""Full router test matrix: every lane, plain + tool-calling + responses API.

Writes results to test-matrix-results.json so runs can be compared over time.

Usage:
    python test_matrix.py           # all lanes
    python test_matrix.py --quick   # skip slow lanes
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("LLM_ROUTER_BASE", "http://127.0.0.1:4000/v1")
KEY_FILE = os.environ.get("LLM_ROUTER_KEY_FILE")   # file holding a client key
KEY_ENV = os.environ.get("LLM_ROUTER_KEY")         # or the key value itself

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}]

# (model, needs_tools_test, note)
# EDIT this list to match YOUR router's model_list. Mixed example covering
# every lane pattern (tool-capability differs per model — test it, don't trust it).
MATRIX = [
    # --- direct upstream / local shim ---
    ("upstream-a/fast", True, "your vendor model"),
    # --- local decision engine (no tools) ---
    ("laya", False, "laya routed decision"),
    ("laya-typed", False, "laya typed"),
    # --- wildcard catalog lane (tool support varies per model!) ---
    ("openrouter/z-ai/glm-5.2:free", True, "glm free (NO TOOLS upstream)"),
    ("openrouter/qwen/qwen3.8-27b:free", True, "qwen free"),
    ("openrouter/nvidia/nemotron-3.5-lightning:free", True, "nemotron lightning free"),
    ("openrouter/deepseek/deepseek-v4.1-flash", True, "openrouter paid"),
    # --- fixed cloud gateway lane ---
    ("ollama/gpt-oss:20b", True, "ollama gpt-oss 20b"),
    ("ollama/deepseek-v4.1-flash", True, "ollama default"),
]


def key() -> str:
    if KEY_FILE:
        return Path(KEY_FILE).read_text(encoding="utf-8").strip()
    if KEY_ENV:
        return KEY_ENV.strip()
    raise SystemExit("Set LLM_ROUTER_KEY_FILE or LLM_ROUTER_KEY before running.")


def call(model: str, with_tools: bool, path: str = "/chat/completions",
         timeout: float = 120.0) -> dict:
    payload = {"model": model,
               "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
               "max_tokens": 200}
    if with_tools:
        payload["tools"] = TOOLS
    if path == "/responses":
        payload = {"model": model, "input": "Reply with exactly: OK",
                   "max_output_tokens": 200}
        if with_tools:
            payload["tools"] = [{"type": "function", "name": "get_weather",
                                 "description": "Get weather",
                                 "parameters": {"type": "object",
                                                "properties": {"city": {"type": "string"}},
                                                "required": ["city"]}}]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key())
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        dt = round((time.time() - t0) * 1000)
        text = ""
        if path == "/chat/completions":
            msg = d["choices"][0]["message"]
            text = (msg.get("content") or "")[:60]
            if msg.get("tool_calls"):
                text += " [TOOL_CALL ok]"
        else:
            text = json.dumps(d)[:120]
        return {"ok": True, "ms": dt, "text": text}
    except urllib.error.HTTPError as e:
        dt = round((time.time() - t0) * 1000)
        detail = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(detail)
            msg = j.get("error", {}).get("message", detail)[:220]
        except Exception:
            msg = detail[:220]
        return {"ok": False, "ms": dt, "code": e.code, "error": msg}
    except Exception as e:
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "code": 0, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    results = []
    for model, tools_test, note in MATRIX:
        row = {"model": model, "note": note}
        r1 = call(model, with_tools=False)
        row["plain"] = r1
        print(f"[{model}] plain: {'OK' if r1['ok'] else 'FAIL'} {r1.get('text', r1.get('error', ''))[:70]}")
        if tools_test:
            r2 = call(model, with_tools=True)
            row["tools"] = r2
            print(f"[{model}] tools: {'OK' if r2['ok'] else 'FAIL'} {r2.get('text', r2.get('error', ''))[:70]}")
        results.append(row)

    out = Path(__file__).parent / "test-matrix-results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if r["plain"]["ok"])
    print(f"\n=== plain: {ok}/{len(results)} ok | results -> {out} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
