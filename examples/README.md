# Examples — Client-Side Configs

Sanitized copies of the client configs that connect to the router. Replace
`<router-key>` with a real per-agent virtual key.

| File | Goes to | What |
|---|---|---|
| `codex-router.config.toml` | `~/.codex/router.config.toml` | Codex CLI profile layering over your main config |
| `codex-router.bat` / `codex-router` | `~/.local/bin/` | Wrapper scripts (`codex --profile router "$@"`) |
| `hermes-router-setup.sh` | run once | Hermes profile config commands |

See `docs/04-CLIENTS.md` for the full walkthrough of each.

## The key rule for every client

One virtual key per client, never the master key. After adding a router lane,
update every client key's allowlist (`docs/01-INSTALL.md` §4).
