# 00 — Overview & Architecture

## What this is

A single local endpoint (`http://127.0.0.1:4000/v1`, OpenAI-compatible) that fronts
**every model you use**, from any provider, with:

- **One URL + one key per agent** — clients don't know or care which provider answers.
- **Per-agent attribution** — every request is logged against the virtual key that
  made it, so you can see who spent what.
- **Retries + failover** — transient upstream failures are absorbed centrally.
- **Live catalogs** — wildcard lanes mean new upstream models work the moment they
  exist, with no config change.

## The five lane patterns

A "lane" is one way models get onto the router. Every real setup is a mix of these:

| Pattern | When to use | Config shape | Example in this repo |
|---|---|---|---|
| **Direct upstream** | A provider (or local shim) speaks plain OpenAI chat | `model: openai/<id>` + `api_base` | `upstream-a/fast` |
| **Wildcard catalog** | A provider has hundreds of models and adds more constantly | `model_name: "provider/*"`, `model: "provider/*"` | `openrouter/*` |
| **Fixed cloud gateway** | A hosted gateway with a stable model list | wildcard + `api_base` | `ollama/*` |
| **Local model server** | A model runs on your own machine/GPU | `api_base: http://127.0.0.1:<port>/v1` | `laya*` |
| **Custom provider** | The API validates *request shape* (required fields, forced streaming, auth quirks) | Python `CustomLLM` handler registered via `custom_provider_map` | `my_provider.py` |

Most setups need patterns 1–4 only. Pattern 5 exists because some APIs reject
"normal" OpenAI-shaped requests until you add specific fields — see
`docs/02-LANES.md` → Custom providers.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ clients                                                             │
│   Codex CLI ─┐                                                      │
│   Hermes  ───┼──► http://127.0.0.1:4000/v1  +  per-agent key        │
│   scripts ───┤                                                      │
│   any SDK ───┘                                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LiteLLM proxy                                                       │
│   • auth (virtual keys)   • routing   • retries (num_retries)       │
│   • drop_params (per-lane param tolerance)                          │
│   • spend logging                                                   │
├─────────────────────────────────────────────────────────────────────┤
│  lane: upstream-a     → http://127.0.0.1:9001/v1   (your provider)  │
│  lane: openrouter/*   → https://openrouter.ai       (400+ models)   │
│  lane: ollama/*       → https://ollama.com/v1       (~20 models)    │
│  lane: laya           → http://127.0.0.1:4030/v1    (local GPU)     │
│  lane: myprov (custom) → shape-shaping Python handler               │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Postgres (container)  →  virtual keys · spend logs · model registry │
└─────────────────────────────────────────────────────────────────────┘
```

## Key design decisions (and why)

1. **Postgres from the start.** LiteLLM can run stateless, but then you have no
   virtual keys and no spend history. A single `postgres:16-alpine` container is
   enough, and `store_model_in_db: true` makes runtime-registered models survive
   restarts.

2. **`drop_params: true` is non-negotiable in a multi-lane setup.** Clients
   (especially CLI agents) send parameters like `reasoning_effort` that some
   upstreams accept and others 400 on. Without this setting, one lane's
   incompatibility breaks the whole request path for that model. With it, each
   lane quietly ignores what it doesn't understand.

3. **Wildcards over explicit model lists.** `openrouter/*` resolves *any* model id
   live. A catalog that changes weekly needs no maintenance — and new models are
   usable the moment they appear. Explicit lists are for small, stable catalogs.

4. **127.0.0.1 in every DB URL, never `localhost`.** On Windows, `localhost` can
   resolve to IPv6 `::1` first; a Postgres listening on IPv4-only refuses it. This
   one substitution prevents a class of "works on my machine" connection bugs.

5. **Secrets as files or env, never in config.** The YAML references
   `os.environ/NAME` only. Launchers read small secret files at start
   (`set -a; . ./.env`). Nothing secret is ever committed.

6. **Local servers are just more lanes.** A model on your GPU is exposed through a
   tiny HTTP wrapper that speaks OpenAI chat, then added as a normal lane. No
   special-casing in clients.

## What's in the rest of these docs

- **01-INSTALL** — start from zero: deps, DB, first run, first key, first call.
- **02-LANES** — each lane pattern step by step, including custom providers.
- **03-JEV-AND-LAYA** — decision engines, their non-chat APIs, and the adapters.
- **04-CLIENTS** — real client recipes (Codex, Hermes, Claude Code, OpenCode).
- **05-OPERATIONS** — restart survival, watchdogs, keeping it alive unattended.
- **06-PITFALLS** — the collected "cost hours" list.
