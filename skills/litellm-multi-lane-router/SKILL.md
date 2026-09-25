---
name: litellm-multi-lane-router
description: "Use when setting up a LiteLLM gateway fronting multiple providers (cloud + local) with per-agent keys — covers lanes, wildcards, custom providers, local model servers, restart survival."
version: 1.0.0
author: Owen
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [litellm, router, gateway, openrouter, ollama, local-models, keys]
    related_skills: [litellm-decision-engines, litellm-router-operations]
---

# LiteLLM Multi-Lane Router — Setup

## Overview

Stand up ONE OpenAI-compatible endpoint (`http://127.0.0.1:4000/v1`) that fronts
every model — cloud APIs, wildcard catalogs, and local GPU models — with
per-agent virtual keys and spend tracking in Postgres.

Serve a human-readable command reference for the whole setup with
`scripts/serve_page.py` (stdlib server + `/health`), then reach it from other
devices via a reverse proxy — map a path prefix (`/ref`) onto the local port.
See `docs/05-OPERATIONS.md` for the ops wiring.

The five lane patterns (full detail in the repo's `docs/02-LANES.md`):

| Pattern | When | Shape |
|---|---|---|
| Direct upstream | Provider is OpenAI-compatible (or you run a shim) | `model: openai/<id>` + `api_base` |
| Wildcard catalog | Big, changing catalog | `model_name: "prov/*"`, `model: "prov/*"` |
| Fixed cloud gateway | Stable list provider | wildcard + `api_base` |
| Local model server | Model runs on your machine | `api_base: http://127.0.0.1:<port>/v1` |
| Custom provider | API validates request SHAPE | Python `CustomLLM` + `custom_provider_map` |

## When to Use

- Setting up LiteLLM for the first time, or extending an existing one with a new provider.
- Wiring CLI agents (Codex, Hermes, Claude Code, OpenCode) onto a shared endpoint.
- "Why does my agent work on one model but 400 on another?" — see pitfalls.
- NOT for: using an already-running router (see `litellm-router-operations`).

## Install order

1. **Postgres first** — keys and spend need it:
   `podman run -d --name litellm-db --restart unless-stopped -p 127.0.0.1:5434:5432 -e POSTGRES_USER=llmrouter -e POSTGRES_PASSWORD=... -e POSTGRES_DB=litellm postgres:16-alpine`
   Use `127.0.0.1` in the DSN, **never `localhost`** (Windows IPv6 pitfall).
2. **Secrets as env/files**, never in YAML: `master_key`, per-provider keys, `db_url`,
   `ui_password`. The config references `os.environ/NAME` only.
3. **config.yaml**: lanes (above) + `litellm_settings`:
   ```yaml
   litellm_settings:
     master_key: os.environ/LITELLM_MASTER_KEY
     drop_params: true                 # multi-lane necessity, see pitfalls
     num_retries: 2
     use_chat_completions_url_for_anthropic_messages: true
   general_settings:
     database_url: os.environ/LITELLM_DATABASE_URL
     store_model_in_db: true           # runtime-registered models survive restarts
   ```
4. **Run**: `litellm --config <yaml> --host 127.0.0.1 --port 4000` (absolute path to
   the CLI when PATH is thin). First cold start runs Prisma migrations — minutes.
5. **Mint per-agent keys**: `POST /key/generate {"key_alias":"<agent>","models":[...]}`.
   Plaintext shown ONCE; save to a locked file. Wildcards allowed (`openrouter/*`).
6. **Verify**: `/health/readiness` → `db:connected`; a real completion per lane.
7. **Serve the command reference** (optional but recommended): `python
   scripts/serve_page.py --root my-commands.html --port 8901`, then proxy it for
   remote access. Autostart-safe (exits quietly if the port is held).

## Iron rules

1. **NEVER put secrets in config files or repos.** Env vars or secret files only.
2. **NEVER hand a client the master key.** One virtual key per agent.
3. **After adding a lane, update EVERY virtual key** — allowlists are fixed at
   mint time; keys do not gain new models automatically (`POST /key/update`).

## Pitfalls (the expensive ones)

| Symptom | Cause | Fix |
|---|---|---|
| One lane 400s on a param others take | Client sends params the lane doesn't know (`reasoning_effort` etc.) | `drop_params: true` |
| New lane 403s on agent keys but works with master | Key allowlist not updated | `POST /key/update` for every key |
| `/v1/models` shows hundreds of phantom names | Provider cost-map noise, not real catalog | Probe real ids; keep only answering ones |
| Readiness `db:disconnected` despite live DB | Query-engine cold-connect flake | Restart the proxy once (automate in launcher) |
| Anthropic clients get empty content | Missing bridge flag | `use_chat_completions_url_for_anthropic_messages: true` |
| Local server responses look empty to Codex/Hermes | Server ignores `stream: true`; clients stream by default | Emit SSE: content chunk + finish + `[DONE]` |
| Free models vanish from keys after a refresh | Refresh treated a 429 as deletion | Classify: RATE = keep, DEAD (404/410) = remove only |
| Local lane names stripped from keys by a refresh | Key sync rebuilt from its own models only | Union with ALL registered model names |

## Adding a LOCAL model server lane

1. Run the model behind a small HTTP server on `127.0.0.1:<port>` speaking
   OpenAI chat. Requirements: bind port BEFORE loading models, preload in a
   background thread, support SSE when `stream: true`.
2. Add a YAML lane: `model: openai/<alias>`, `api_base: http://127.0.0.1:<port>/v1`,
   `use_chat_completions_api: true`. One entry per checkpoint alias.
3. Update keys, restart proxy once, verify with a real call.

`scripts/laya_server.py` in the repo is a complete reference implementation.

## Common pitfalls (process)

1. **Reusing the master key everywhere** — kills attribution and blast-radius control.
2. **Testing with `curl` only against the proxy** — also test through your actual
   client; streaming behavior differs (see SSE pitfall).
3. **Assuming a restarted VM brings containers back** — it doesn't by default;
   see `litellm-router-operations`.
4. **Committing `.env` or key files** — gitignore them from commit #1.

## Verification checklist

- [ ] `/health/readiness` → `{"db":"connected"}`
- [ ] `/v1/models` lists your lanes
- [ ] A virtual key (not master) completed a call and the spend shows in the dashboard
- [ ] Every key updated after the last lane addition
- [ ] A real client (Codex/Hermes/etc.) answered through the router
- [ ] Secrets files are gitignored and icacls/chmod-locked
