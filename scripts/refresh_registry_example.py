#!/usr/bin/env python3
"""SKELETON: dynamic model-registry refresh with the correct probe semantics.

Copy and adapt. The point of this file is the CLASSIFICATION LOGIC, which took
a real bug to learn:

    OK      probe succeeded                -> keep / register
    RATE    HTTP 429 (quota / rate limit)  -> KEEP REGISTERED (the model is
                                               alive; quota windows reset)
    DEAD    HTTP 404/410 (retired)         -> remove
    UNKNOWN transient/other/network        -> leave everything untouched

THE BUG TO AVOID: treating a 429 as "dead" evicts WORKING models and strips
them from agent keys on every refresh. And when syncing keys, union your
registry with ALL other registered model names — a refresh that rebuilds key
allowlists from only its own models silently drops every other lane (e.g. a
local decision engine).

Adapt the three marked functions to your provider; the loop shape stays.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("LLM_ROUTER_BASE", "http://127.0.0.1:4000")
MASTER = os.environ.get("LLM_ROUTER_MASTER_KEY", "").strip()


def ll_api(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + MASTER)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


# ---- ADAPT #1: which upstream models should exist? --------------------------
def upstream_model_ids() -> list[str]:
    """Return the provider's current model ids. Example: a public catalog."""
    try:
        with urllib.request.urlopen(
                "https://api.provider.example/v1/models", timeout=30) as r:
            d = json.load(r)
        return [m["id"] for m in d.get("data", []) if m["id"].endswith("-free")]
    except Exception as e:
        print(f"! catalog fetch failed: {e}")
        return []


# ---- ADAPT #2: probe one model through the router ---------------------------
def probe(model_id: str) -> str:
    """Return one of OK / RATE / DEAD / UNKNOWN by calling through the router."""
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
    }
    st, _ = ll_api("/v1/chat/completions", "POST", payload)
    if st == 200:
        return "OK"
    if st == 429:
        return "RATE"
    if st in (404, 410):
        return "DEAD"
    return "UNKNOWN"


# ---- ADAPT #3: registration payload ----------------------------------------
def register(name: str, upstream_id: str):
    return ll_api("/model/new", "POST", {
        "model_name": name,
        "litellm_params": {
            "model": f"<provider>/{upstream_id}",   # ADAPT provider prefix
            "api_key": os.environ.get("PROVIDER_API_KEY", ""),
        },
    })


def registered_models() -> dict[str, dict]:
    st, body = ll_api("/model/info")
    if st != 200:
        return {}
    out = {}
    for m in json.loads(body).get("data", []):
        out[m.get("model_name")] = m
    return out


def sync_keys(managed_names: set[str]):
    """Grant every virtual key: managed models UNION all other registered
    names (so other lanes — local engines, wildcards — are never dropped)."""
    st, body = ll_api("/model/info")
    all_names = {m.get("model_name") for m in json.loads(body).get("data", [])
                 if m.get("model_name")} if st == 200 else set(managed_names)
    st, body = ll_api("/key/list")
    if st != 200:
        return
    for tok in json.loads(body).get("keys", []):
        ll_api("/key/update", "POST",
               {"key": tok, "models": sorted(all_names)})


def main() -> int:
    if not MASTER:
        sys.exit("Set LLM_ROUTER_MASTER_KEY")

    want = {f"<prefix>-{mid}": mid for mid in upstream_model_ids()}  # ADAPT naming
    if not want:
        print("catalog unavailable; leaving registrations untouched")
        return 1

    verdicts = {mid: probe(f"<prefix>-{mid}") for mid in want.values()}
    working = [mid for mid, v in verdicts.items() if v == "OK"]

    have = registered_models()
    added = removed = 0
    for name, mid in want.items():
        if name not in have and mid in working:
            st, _ = register(name, mid)
            added += st == 200
    for name, meta in have.items():
        upstream = (meta.get("litellm_params") or {}).get("model", "").split("/")[-1]
        if name.startswith("<prefix>-") and verdicts.get(upstream) == "DEAD":
            st, _ = ll_api("/model/delete", "POST", {"id": meta.get("id")})
            removed += st == 200

    sync_keys(set(have) | set(want))
    print(f"+{added} registered, -{removed} removed, keys synced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
