# LLM Router

**One OpenAI-compatible endpoint for every model you use — cloud APIs, free tiers, wildcard catalogs, and fully local models — with per-agent keys, spend tracking, and restart survival.**

Built with [LiteLLM](https://github.com/BerriAI/litellm) as the gateway. This repo is the
battle-tested setup: the exact config shape, the scripts, the pitfalls, and the docs
for wiring real clients (Codex CLI, Hermes Agent, Claude Code, OpenCode) onto it.

```
clients (CLI agents · scripts · any OpenAI client)
   │   http://127.0.0.1:4000/v1  +  a per-agent virtual key
   ▼
LiteLLM  ──────────────  auth · routing · retries · spend logs
   ├── Lane: direct upstream      (any OpenAI-compatible provider / local shim)
   ├── Lane: wildcard catalog     (openrouter/* — any model, new ones included)
   ├── Lane: second cloud gateway (ollama/* and similar fixed catalogs)
   ├── Lane: LOCAL decision engine (laya — typed decisions in one forward pass)
   └── Lane: custom provider      (APIs that validate request SHAPE)
   ▼
Postgres (container)  ←  keys · spend logs · model registry
```

## Why a router at all

- **One key, one URL** for every agent, instead of N provider SDKs and N key files.
- **Per-agent attribution** — each client gets its own virtual key; spend and usage
  are logged per key.
- **Model failover and retries** in one place.
- **Wildcard lanes** mean brand-new upstream models work *immediately* — no config
  edit, no restart.
- **Local models ride the same API** as cloud ones — swap between a 30 ms local
  decision engine and a frontier cloud model with just a model name.

## Contents

| Path | What |
|---|---|
| `config.example.yaml` | The router config: five lane patterns, all placeholders |
| `.env.example` | Every environment variable the config expects |
| `docs/00-OVERVIEW.md` | Architecture and design decisions |
| `docs/01-INSTALL.md` | Install + first run, step by step |
| `docs/02-LANES.md` | Adding lanes: upstream, wildcard, local, custom provider |
| `docs/03-JEV-AND-LAYA.md` | Decision engines: **Jev** (cloud, `systemone` API) and **Laya** (local, Apache-2.0) — adapters, question types, side-by-side comparison |
| `docs/04-CLIENTS.md` | Wiring Codex CLI, Hermes Agent, Claude Code, OpenCode |
| `docs/04b-CLAUDE-CODE.md` | **Claude Code in depth** — the Messages-API path, launcher pattern, tool-use verification, context-window fix |
| `docs/05-OPERATIONS.md` | Restart survival, watchdogs, port healing, recovery |
| `docs/06-PITFALLS.md` | The mistakes that cost hours, and their fixes |
| `docs/08-DASHBOARD.md` | **Filterable live dashboard** — tabs, filters, the slow-spend-log fix, serving other pages |
| `docs/09-RESPONSES-STREAMING-FIX.md` | **Codex `OutputTextDelta without active item`** — root cause (reasoning-first streams) + idempotent backport patch (`scripts/patch_litellm_responses_stream.py`) |
| `docs/10-CODEX-SESSION-RESUME.md` | **Old Codex sessions won't resume** — wrong profile, Ollama's null-content rejection for replayed reasoning items, and the idempotent patch (`scripts/patch_litellm_null_content.py`) + regression test |
| `scripts/` | Launcher, health-heal, local decision server, refresh/watchdog helpers |
| `skills/` | Agent skills (Hermes/Codex SKILL.md format) — setup + operations |
| `examples/` | Consumer-side files (Codex profile, wrappers) |

## Quickstart

```bash
git clone https://github.com/THEROCKSSS/llm-router.git && cd llm-router
cp .env.example .env                       # fill in your keys
cp config.example.yaml litellm.yaml        # point it at your lanes

# 1) Postgres for keys + spend logs (podman or docker)
podman run -d --name litellm-db -p 127.0.0.1:5434:5432 \
  -e POSTGRES_USER=llmrouter -e POSTGRES_PASSWORD=CHANGE_ME \
  -e POSTGRES_DB=litellm postgres:16-alpine

# 2) run the proxy
set -a; . ./.env; set +a
litellm --config litellm.yaml --host 127.0.0.1 --port 4000

# 3) verify
curl -s http://127.0.0.1:4000/health/readiness
# {"status":"healthy","db":"connected"}
```

Then mint a key for a client and make your first call — see `docs/01-INSTALL.md`.

## The two decision-engine recipes

This repo includes full documentation for running **typed-decision** models — models
that answer structured questions (`choice` / `score` / yes-no) in one shot instead of
generating text:

- **Jev** (cloud, closed API): its endpoint is *not* chat — it takes
  `{state, questions}` and returns typed answers. The repo documents the adapter that
  makes it behave like a chat model on the router, plus its question types.
- **Laya** (local, Apache-2.0, `pip install laya`): a non-autoregressive engine that
  runs in ~30 ms on a modest GPU. The repo ships the local HTTP wrapper and shows the
  router lanes (`laya`, `laya-en`, `laya-ml`, `laya-typed`) plus a side-by-side
  comparison script.

See `docs/03-JEV-AND-LAYA.md`.

## Security notes

- **No secrets in this repo, ever.** All keys live in `.env` (gitignored) or in
  per-secret files read at launch. `config.example.yaml` references env vars only.
- Per-agent virtual keys are preferred over sharing the master key.
- The proxy binds `127.0.0.1` by default. If you expose it on a network, put it
  behind TLS and rotate keys.

## License

MIT (this repo's own code). Upstream projects keep their own licenses — see each
vendor's terms, and note that some free tiers have usage caps that reset daily.
