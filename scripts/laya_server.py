#!/usr/bin/env python3
"""Laya decision service — local HTTP wrapper around the Laya SDK.

Runs INSIDE the laya-svc venv (torch/transformers/laya), serving an
OpenAI-compatible chat endpoint so LiteLLM can route to it like any lane:

    LiteLLM :4000  ->  laya_server :4030  ->  Router(preload=True)

Model names map to checkpoints:
    laya          -> Router (routed: english/multilingual/typed-decisions)
    laya-en       -> english checkpoint
    laya-ml       -> multilingual checkpoint
    laya-typed    -> typed-decisions checkpoint

Input contract:
  - last user message text is the STATE
  - if that text is JSON {"state": str, "questions": {...}} -> used verbatim
  - otherwise default questions = worth_doing (noul) + confidence (score).

Output: a single rendered decision block (text), e.g.

    Laya decision (laya / routed: english)
    • worth_doing: YES (96% yes)
    • confidence: 1.84  [low 5%, medium 20%, high 75%]
    usage: 42 in / 0 out · cost $0

Streaming is supported: when `stream:true`, emit one real SSE content chunk, a
finish chunk, usage, and `data: [DONE]`. Empty deltas make Hermes/LiteLLM treat
the response as blank.

The port binds before the GPU checkpoints finish. `/health` therefore returns
200 while inference is still blocked on the model-load lock. Use `/info` and wait
for `meta.laya_version` as the real readiness gate.

Extra native endpoint: POST /predict {state, questions} -> raw JSON answers.

Env: LAYA_PORT (default 4030), LAYA_DEVICE (auto-> best CUDA or cpu),
     LAYA_PRELOAD (default 1).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LAYA_PORT", "4030"))
PRELOAD = os.environ.get("LAYA_PRELOAD", "1") not in ("0", "false", "no")

DEFAULT_QUESTIONS = {
    "worth_doing": {
        "type": "noul",
        "instructions": "Is the action or change described in the state worth doing?",
    },
    "confidence": {
        "type": "score",
        "instructions": "How confident is this judgment?",
        "criteria": ["low", "medium", "high"],
    },
}

MODEL_ALIASES = {
    "laya": None,                      # routed (None = Router default)
    "laya-en": "english",
    "laya-english": "english",
    "laya-ml": "multilingual",
    "laya-multilingual": "multilingual",
    "laya-typed": "typed-decisions",
    "laya-typed-decisions": "typed-decisions",
}

_router = None
_router_meta: dict = {}
_load_lock = threading.Lock()


def _pick_device() -> str:
    forced = os.environ.get("LAYA_DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            # pick the GPU with the most VRAM (multi-GPU hosts: 1650+3060)
            best, best_mem = 0, -1
            for i in range(torch.cuda.device_count()):
                mem = torch.cuda.get_device_properties(i).total_memory
                if mem > best_mem:
                    best, best_mem = i, mem
            name = torch.cuda.get_device_name(best)
            return f"cuda:{best}" if torch.cuda.device_count() > 1 else "cuda"
        return "cpu"
    except Exception:
        return "cpu"


def get_router():
    global _router, _router_meta
    with _load_lock:
        if _router is None:
            import laya
            from laya import Router
            device = _pick_device()
            t0 = time.time()
            _router = Router(preload=PRELOAD, device=device)
            _router_meta = {
                "device": device,
                "preload": PRELOAD,
                "load_seconds": round(time.time() - t0, 1),
                "laya_version": getattr(laya, "__version__", "?"),
            }
        return _router


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
            if isinstance(c, list):
                parts = [p.get("text", "") for p in c
                         if isinstance(p, dict) and isinstance(p.get("text"), str)]
                joined = "\n".join(parts).strip()
                if joined:
                    return joined
    return ""


def _parse_state_questions(text: str) -> tuple[dict, dict]:
    """text -> (state, questions). JSON {state:str, questions:dict} used verbatim."""
    if text.strip().startswith("{"):
        try:
            j = json.loads(text)
            if isinstance(j, dict) and isinstance(j.get("questions"), dict):
                st = j.get("state")
                state = {"state": st} if isinstance(st, str) else (st if isinstance(st, dict) else {"state": text})
                return state, j["questions"]
        except Exception:
            pass
    return {"state": text}, dict(DEFAULT_QUESTIONS)


def _pct(v) -> str:
    try:
        return f"{round(float(v or 0) * 100)}%"
    except Exception:
        return "0%"


def _render(model_name: str, routed_model: str | None, reason: str | None,
            answers: dict) -> str:
    head = f"Laya decision ({model_name}"
    if routed_model:
        head += f" / routed: {routed_model}"
    head += ")"
    lines = [head]
    for qid, a in (answers or {}).items():
        if not isinstance(a, dict):
            lines.append(f"• {qid}: {json.dumps(a)[:200]}")
            continue
        t = a.get("type")
        if t == "noul":
            p = a.get("noul")
            try:
                pf = float(p)
            except Exception:
                pf = 0.0
            lines.append(f"• {qid}: {'YES' if pf >= 0.5 else 'NO'} ({_pct(pf)} yes)")
        elif t == "choice":
            probs = ""
            if isinstance(a.get("probabilities"), dict):
                probs = ", ".join(
                    f"{k} {_pct(v)}"
                    for k, v in sorted(a["probabilities"].items(), key=lambda kv: -float(kv[1] or 0))
                )
            conf = a.get("confidence")
            cpart = f"  [conf {_pct(conf)}]" if conf is not None else ""
            lines.append(f"• {qid}: {a.get('choice', '?')}" + cpart + (f"  [{probs}]" if probs else ""))
        elif t == "score":
            legend = a.get("legend") or {}
            probs = ""
            if isinstance(a.get("probabilities"), dict):
                try:
                    items = sorted(a["probabilities"].items(), key=lambda kv: int(kv[0]))
                except Exception:
                    items = list(a["probabilities"].items())
                probs = ", ".join(f"{legend.get(k, k)} {_pct(v)}" for k, v in items)
            sc = a.get("score")
            scs = f"{float(sc):.2f}" if isinstance(sc, (int, float)) else str(sc)
            lines.append(f"• {qid}: {scs}" + (f"  [{probs}]" if probs else ""))
        else:
            # unknown shape: surface choice/score/noul if present, else json
            got = {k: a[k] for k in ("choice", "score", "noul", "confidence") if k in a}
            lines.append(f"• {qid}: {json.dumps(got or a)[:300]}")
    return "\n".join(lines)


def run_predict(state: dict, questions: dict, model_name: str) -> dict:
    """Core call — returns {'text': rendered, 'raw': answers, 'routing': {...}}."""
    router = get_router()
    forced = MODEL_ALIASES.get(model_name, None)
    t0 = time.time()
    if forced is None:
        res = router.predict(state, questions)
    else:
        res = router.predict(state, questions, model=forced)
    elapsed_ms = round((time.time() - t0) * 1000)
    answers = res.get("answers", res)
    routing = res.get("routing") or {}
    text = _render(model_name, routing.get("model"), routing.get("reason"), answers)
    raw = res
    return {"text": text, "raw": raw, "routing": routing, "latency_ms": elapsed_ms}


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server that ignores client disconnects during long cold loads.

    LiteLLM/Codex time out while Laya loads its GPU checkpoints. Those clients
    close the socket, and http.server otherwise prints a full ConnectionReset
    traceback for every timeout, burying real startup errors in laya.log.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return
        if path == "/v1/models":
            self._send_json({"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "laya-svc"} for m in MODEL_ALIASES
            ]})
            return
        if path == "/info":
            self._send_json({"service": "laya-svc", "port": PORT, "meta": _router_meta})
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/predict"):
            self._send_json({"error": "not found"}, 404)
            return
        payload = self._read_body()
        try:
            if path == "/predict":
                state = payload.get("state") or {}
                questions = payload.get("questions") or dict(DEFAULT_QUESTIONS)
                if isinstance(state, str):
                    state = {"state": state}
                model_name = payload.get("model") or "laya"
                out = run_predict(state, questions, model_name)
                self._send_json({"answers": out["raw"].get("answers", out["raw"]),
                                 "routing": out["routing"], "latency_ms": out["latency_ms"]})
                return
            # OpenAI-compatible chat completions
            messages = payload.get("messages") or []
            model_name = payload.get("model") or "laya"
            want_stream = bool(payload.get("stream"))
            text = _last_user_text(messages)
            state, questions = _parse_state_questions(text or "(empty)")
            out = run_predict(state, questions, model_name)
            prompt_tokens = max(1, len(text) // 4)
            cid = "chatcmpl-laya-" + str(int(time.time() * 1000))
            created = int(time.time())
            if want_stream:
                # Laya is non-autoregressive: one content chunk, then a finish
                # chunk + [DONE]. Emitting real SSE is REQUIRED — clients like
                # Hermes stream by default, and without this LiteLLM produces a
                # delta-less chunk that reads as an empty response.
                def _sse(obj) -> bytes:
                    return ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                def _chunk(delta: dict, finish=None):
                    return {
                        "id": cid, "object": "chat.completion.chunk",
                        "created": created, "model": model_name,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }

                try:
                    self.wfile.write(_sse(_chunk({"role": "assistant", "content": out["text"]})))
                    self.wfile.write(_sse(_chunk({}, "stop")))
                    self.wfile.write(_sse({
                        "id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": model_name, "choices": [],
                        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 0,
                                  "total_tokens": prompt_tokens},
                    }))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return
            self._send_json({
                "id": "chatcmpl-laya-" + str(int(time.time() * 1000)),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_name,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": out["text"]},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 0,
                          "total_tokens": prompt_tokens},
            })
        except Exception as e:
            tb = traceback.format_exc()[-800:]
            self._send_json({"error": {"message": f"{type(e).__name__}: {e}\n{tb}"}}, 500)


def main() -> int:
    # Bind the HTTP port FIRST so health checks answer immediately; preload the
    # checkpoints in a background thread. A cold load can take several minutes
    # (observed ~390s); requests arriving during preload block on get_router()'s
    # lock until the checkpoint is ready.
    srv = QuietThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def _preload():
        try:
            get_router()
            sys.stderr.write(f"laya-svc: ready ({_router_meta})\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"laya-svc: preload failed ({e}); will retry lazily\n")
            sys.stderr.flush()

    if PRELOAD:
        threading.Thread(target=_preload, daemon=True).start()
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
