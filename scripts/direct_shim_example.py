#!/usr/bin/env python3
"""EXAMPLE: minimal OpenAI-shape shim in front of a NON-OpenAI vendor API.

Pattern (docs/02-LANES.md, Pattern 1): when your upstream isn't
OpenAI-compatible, run a tiny local shim that translates both directions and
point a router lane at it:

    client -> LiteLLM :4000 -> this shim :9001 -> https://api.vendor.example

The shim needs only two things: accept /v1/chat/completions, translate to the
vendor's request shape, translate the response back. ~120 lines with FastAPI.

Install:  pip install fastapi uvicorn httpx
Run:      VENDOR_API_KEY=... python direct_shim_example.py     # listens :9001
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

VENDOR_BASE = os.environ.get("VENDOR_BASE_URL", "https://api.vendor.example")
KEY = os.environ.get("VENDOR_API_KEY", "").strip()
PORT = int(os.environ.get("SHIM_PORT", "9001"))

app = FastAPI()


def _vendor_payload(openai_body: dict) -> dict:
    """Translate an OpenAI chat body into the vendor's shape.

    ADAPT THIS. Typical needs:
      - merge all system/developer messages into ONE (some upstreams accept
        exactly one, and it must be first)
      - rename fields (max_tokens -> max_new_tokens, etc.)
      - drop fields the vendor rejects
    """
    msgs = openai_body.get("messages") or []
    system_parts = [m.get("content", "") for m in msgs
                    if m.get("role") in ("system", "developer")]
    convo = [m for m in msgs if m.get("role") not in ("system", "developer")]
    merged: list[dict[str, Any]] = []
    if system_parts:
        merged.append({"role": "system", "content": "\n\n".join(
            p if isinstance(p, str) else str(p) for p in system_parts)})
    merged.extend(convo)
    return {
        "model": openai_body.get("model"),
        "messages": merged,
        "max_new_tokens": openai_body.get("max_tokens"),
        "stream": bool(openai_body.get("stream")),
    }


def _vendor_response_to_openai(vendor: dict, model: str) -> dict:
    """Translate the vendor's reply into an OpenAI chat.completion.

    ADAPT THIS. Keep the id/created/choices/usage skeleton — clients rely on it.
    """
    text = (vendor.get("output") or vendor.get("text") or "").strip()
    return {
        "id": vendor.get("id", "chatcmpl-shim"),
        "object": "chat.completion",
        "created": vendor.get("created", 0),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": vendor.get("input_tokens", 0),
                  "completion_tokens": vendor.get("output_tokens", 0),
                  "total_tokens": (vendor.get("input_tokens", 0)
                                   + vendor.get("output_tokens", 0))},
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    # NOTE: build the header key at runtime; a literal '"authorization": "Bearer"'
    # in tool-written files can get display-masked into broken content.
    headers = {"Content-Type": "application/json"}
    headers["authori" + "zation"] = "Bearer " + KEY

    payload = _vendor_payload(body)
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(f"{VENDOR_BASE}/v1/generate", json=payload, headers=headers)
    if r.status_code != 200:
        return JSONResponse({"error": {"message": r.text[:500]}},
                            status_code=r.status_code)

    if not body.get("stream"):
        return JSONResponse(_vendor_response_to_openai(r.json(), body.get("model", "")))

    # Streaming path: translate vendor SSE into OpenAI SSE chunks.
    async def gen():
        import json as _json
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            chunk = {"id": "chatcmpl-shim", "object": "chat.completion.chunk",
                     "model": body.get("model", ""),
                     "choices": [{"index": 0, "delta": {"content": line + "\n"},
                                  "finish_reason": None}]}
            yield f"data: {_json.dumps(chunk)}\n\n"
        yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/health")
def health():
    return {"ok": bool(KEY)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
