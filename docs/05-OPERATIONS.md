# 05 — Operations & Restart Survival

A router that doesn't come back after a reboot is a toy. This page is the
difference between "it works when I run it" and "it's always there."

---

## The three-layer survival model

Each layer covers a different failure mode:

| Layer | Covers | Mechanism |
|---|---|---|
| 1. Container restart policy | The DB container dying/rebooting | `--restart unless-stopped` |
| 2. Runtime boot restore | The *container runtime itself* restarting (VM boots empty) | Enable the runtime's boot-restore service |
| 3. Logon autostart + watchdog | The proxy and local model servers — they aren't containers | Startup script + periodic health check that heals |

### Layer 1 — Container restart policy

```bash
podman run -d --name litellm-db --restart unless-stopped …
# already running? set it live — no recreate needed:
podman update litellm-db --restart unless-stopped
```

### Layer 2 — Boot restore (Podman machine / VM runtimes)

If the container runtime runs inside a VM (Podman machine on Windows/macOS),
containers do **not** come back by default after a host reboot. The VM's own
systemd service `podman-restart.service` does the restore — it must be enabled,
and it needs hardening:

```bash
# inside the VM
podman machine ssh
sudo chown -R $USER:$USER ~/.config/systemd   # may be root-owned on fresh images
systemctl --user enable podman-restart.service
```

**The hardening drop-in is mandatory** — distro images ship
`TimeoutStopFailureMode=abort`, and when the machine shuts down, systemd
SIGABRTs the whole service cgroup mid-restore, killing the container
port-forwarders. The result: containers show **"running"** after boot but their
**ports refuse connections**. Drop-in at
`~/.config/systemd/user/podman-restart.service.d/override.conf`:

```ini
[Service]
TimeoutStartSec=infinity
TimeoutStopSec=300
ExecStart=
ExecStart=-/usr/bin/podman $LOGGING start --all --filter should-start-on-boot=true
ExecStop=
KillMode=none
```

(`ExecStart=` clears the original first, then re-adds it with a `-` so failures
don't kill the unit; `KillMode=none` stops the SIGABRT storm; the timeouts stop
systemd from killing a legitimately slow restore. Verify merged config with
`systemctl --user cat podman-restart.service`.)

### Layer 3 — Autostart + watchdog

The proxy and local model servers live **outside** containers, so a startup
script starts them at logon:

```
Startup folder → launcher (.vbs/.bat) → start script
    1. wait for container runtime API to answer
    2. start the DB container
    3. restore boot containers (twice — first pass can fail transitively)
    4. heal dead port-forwards  (see below)
    5. start the proxy; wait for /health/liveliness
    6. start local model servers (e.g. laya on :4030)
    7. refresh dynamic model registries
```

Plus a **watchdog** every few hours: health-check every service; if any is down,
attempt `start script` once, then heal container ports and retry; alert only if
still down. Silent when healthy.

**Readiness is stronger than port health.** Two services answer a basic health
check before they are actually usable:

- **LiteLLM** can serve `/health/liveliness` while Prisma reports
  `db: disconnected`. Require `/health/readiness` to contain
  `{"status":"healthy","db":"connected"}` before treating it as up.
- **Laya** binds `:4030` before the GPU checkpoints finish loading. Require
  `/info` to contain `laya_version`; `/health` alone returns 200 while requests
  are blocked on the model-load lock.

Client timeouts during a long cold preload are expected. The wrapper suppresses
only `BrokenPipe`/`ConnectionReset` tracebacks; real startup failures still reach
the log.

---

## Port-forward healing (the subtle killer)

After a hard stop/start of the container VM, a container can be **"running" with
its published port dead** — the forwarder process didn't come back. A plain
`podman start` is a **no-op** on a running container, so it never heals.

`scripts/heal_container_ports.py` (in this repo) fixes it:

1. List running containers and parse their published ports.
2. Probe every `host:port` (socket connect, short timeout).
3. For dead ones only: `stop -t 15 <name>` then `start <name>` — this rebuilds
   the forward.
4. Report; exit non-zero if any remain dead.

Run it after any runtime restart, or wire it into the autostart script and the
watchdog's second-line recovery (both done in this repo's scripts).

---

## Health checks that matter

| What | Check |
|---|---|
| Proxy alive | `curl :4000/health/liveliness` |
| Proxy + DB | `curl :4000/health/readiness` → `{"db":"connected"}` |
| Local model server | `curl :<port>/health` |
| End-to-end | a real completion through the router with a client key |

> **Cold start flake:** after a fresh DB start, readiness can briefly show
> `db:disconnected` even though Postgres is fine (the query engine's first
> connect). Restart the proxy once; launchers automate this.

---

## Dynamic registries

If you register models **at runtime** (rather than in YAML), they persist in the
DB across restarts. Keep them fresh with a periodic refresh that:

- probes each registered model and classifies results:
  **OK** → serve; **RATE** (429/quota) → **keep** (it's alive; quota resets);
  **DEAD** (404/410) → remove; **UNKNOWN** → leave untouched.
- syncs virtual keys from the authoritative registry after changes.

> **Critical bug class:** treating a 429 (quota) as death evicts *working*
> models and strips them from agent keys on every refresh. Distinguish
> rate-limited from retired, and never evict on a transient error.

> **Second bug class:** a refresh that rebuilds key allowlists from only *its*
> models silently drops every other lane (e.g. your local decision engine).
> When syncing keys, union the registry with **all other registered model
> names**, not just the ones you manage.

---

## Logging

Keep one log per component (`proxy.log`, `registry-refresh.log`, `autostart.log`,
each local server's log). A watchdog that writes nothing when healthy and a
report when not is the right default — silence means "no news."

## Recovery playbook

1. **Something's down?** Run the start script (idempotent — safe every time).
2. **Proxy up but DB disconnected?** Restart just the proxy.
3. **Containers running but ports dead?** `heal_container_ports.py`.
4. **Runtime API hung?** `podman machine stop && podman machine start` (or the
   Docker Desktop equivalent) — the boot restore brings everything back.
5. **Still broken?** Check the logs in order: autostart → proxy → the specific
   local server.

## Production checklist

- [ ] DB container: `restart=unless-stopped`
- [ ] VM boot-restore service enabled + hardened drop-in applied
- [ ] Autostart script installed and tested by an actual reboot
- [ ] Watchdog scheduled and manually triggered once to see it pass
- [ ] Port-healer wired into both autostart and watchdog
- [ ] Every service has a health endpoint and a log file
