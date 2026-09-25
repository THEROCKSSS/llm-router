# 01 — Install & First Run

## Prerequisites

| Need | Why | Check |
|---|---|---|
| Python 3.10+ | LiteLLM runtime | `python --version` |
| A container runtime (Podman or Docker) | Postgres for keys + spend | `podman --version` |
| One provider API key | To have something to route to | — |

LiteLLM itself runs as a CLI. Install it however you prefer:

```bash
# pipx / uv (recommended — isolated CLI)
uv tool install 'litellm[proxy]'
# or
pipx install 'litellm[proxy]'
# or plain pip
pip install 'litellm[proxy]'
```

Verify: `litellm --version`.

## Step 1 — Postgres

```bash
podman run -d --name litellm-db \
  --restart unless-stopped \
  -p 127.0.0.1:5434:5432 \
  -e POSTGRES_USER=llmrouter \
  -e POSTGRES_PASSWORD=CHANGE_ME \
  -e POSTGRES_DB=litellm \
  postgres:16-alpine
```

Notes:

- Pick any free host port (`5434` avoids colliding with a local Postgres on 5432).
- Bind `127.0.0.1:` so the DB is not exposed on your network.
- `--restart unless-stopped` is the first piece of restart survival — see
  `docs/05-OPERATIONS.md`.
- If the port says "in use" but nothing is listening, a rootless port-forwarder
  died and left an orphan. Check the container runtime's port bindings; recreate
  the container **on the same volume/data dir** rather than fighting the orphan.

## Step 2 — Config + secrets

```bash
git clone https://github.com/THEROCKSSS/llm-router.git && cd llm-router
cp config.example.yaml litellm.yaml
cp .env.example .env
# edit .env: fill in LITELLM_MASTER_KEY, LITELLM_DATABASE_URL, and your lane keys
```

Generate a strong master key:

```bash
LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"
```

`LITELLM_DATABASE_URL` should be:

```
postgresql://llmrouter:CHANGE_ME@127.0.0.1:5434/litellm
```

> **127.0.0.1, not localhost.** On Windows `localhost` may resolve to IPv6 while
> Postgres listens on IPv4 — the connection then fails with a confusing refusal.

## Step 3 — Run the proxy

```bash
set -a; . ./.env; set +a
litellm --config litellm.yaml --host 127.0.0.1 --port 4000
```

First start with a fresh database runs Prisma migrations — that can take a few
minutes. Watch for:

```
LiteLLM: Proxy initialized with Config, Set models:
    <your lanes listed here>
```

Then check both endpoints:

```bash
curl -s http://127.0.0.1:4000/health/liveliness      # → "I'm alive!"
curl -s http://127.0.0.1:4000/health/readiness
# {"status":"healthy","db":"connected"}
```

If readiness says `db:disconnected` despite the container being up, restart the
proxy once — the Prisma query engine occasionally fails its first cold connect.
(Launchers in `scripts/` do this automatically; see `docs/05-OPERATIONS.md`.)

## Step 4 — Mint a client key

Never hand out the master key. Create a virtual key per client:

```bash
MASTER=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2-)

curl -s http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $MASTER" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "my-client",
    "models": ["upstream-a/fast", "openrouter/*", "ollama/*", "laya"]
  }'
```

The response contains the **plaintext key exactly once** — save it now. The
`models` list is the key's allowlist: a key can only call what's listed (wildcards
allowed). Store it as a file or in a password manager, never in the config.

> Adding a lane later? Update every key's allowlist:
> `POST /key/update {"key":"sk-...", "models":[...existing..., "new-lane"]}`.
> Keys are *not* retroactively granted new models.

## Step 5 — First call

```bash
KEY="sk-...your-client-key..."

curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"upstream-a/fast","messages":[{"role":"user","content":"Say OK"}]}'
```

Check the dashboard at `http://127.0.0.1:4000` (log in with the master key, or
`UI_USERNAME`/`UI_PASSWORD` if you set them) — your call should appear with
tokens and cost attributed to the key.

## Step 6 — Make it survive restarts

Three layers, each covering the previous one's failure mode:

1. **Container restart policy** — `--restart unless-stopped` on Postgres (done above).
2. **Boot-time restore** — if your runtime runs in a VM (Podman machine on
   Windows/macOS), enable its container-restore service.
3. **Logon/session autostart** — a script that starts the proxy (and local model
   servers) when the machine boots, plus a periodic watchdog that heals failures.

Full recipes: `docs/05-OPERATIONS.md`.

## Checklist

- [ ] `health/readiness` reports `db:connected`
- [ ] `/v1/models` lists your lanes
- [ ] A virtual key (not the master key) made a successful completion
- [ ] Spend for that call is visible in the dashboard
- [ ] Secrets live outside the repo (`.env` or secret files, gitignored)
- [ ] Autostart + watchdog installed (`docs/05-OPERATIONS.md`)
