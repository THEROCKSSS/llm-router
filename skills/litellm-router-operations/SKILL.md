---
name: litellm-router-operations
description: "Use when keeping a LiteLLM router alive across reboots — restart survival layers, port-forward healing, watchdogs, and recovery drills for container-backed stacks."
version: 1.0.0
author: Owen
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [litellm, operations, restart, podman, watchdog, health]
    related_skills: [litellm-multi-lane-router, litellm-decision-engines]
---

# LiteLLM Router — Operations & Restart Survival

## Overview

A router that doesn't come back after a reboot is a toy. Three layers, each
covering the previous one's failure mode:

| Layer | Covers | Mechanism |
|---|---|---|
| 1. Restart policy | DB container dying | `--restart unless-stopped` (`podman update NAME --restart unless-stopped` sets it live) |
| 2. Runtime boot restore | The container VM restarting empty | Enable + harden `podman-restart.service` inside the machine |
| 3. Autostart + watchdog | Proxy & local model servers (outside containers) | Startup script + periodic health heal |

## Iron rules

1. **Never trust "running" as "reachable"** — after a VM restart a container can
   be running with a DEAD published port. Probe the port, heal if refused.
2. **A container's dead port needs `stop -t 15 NAME` + `start NAME`.** A plain
   `start` is a no-op on a running container and will not rebuild the forwarder.
3. **The boot-restore service MUST be hardened** (below) — without it, systemd's
   abort handler kills the restore mid-run and severs port-forwarders.

## The VM boot-restore fix (Podman machine)

```bash
podman machine ssh
sudo chown -R $USER:$USER ~/.config/systemd          # may be root-owned
systemctl --user enable podman-restart.service
# MANDATORY hardening drop-in:
mkdir -p ~/.config/systemd/user/podman-restart.service.d
cat > ~/.config/systemd/user/podman-restart.service.d/override.conf <<'EOF'
[Service]
TimeoutStartSec=infinity
TimeoutStopSec=300
ExecStart=
ExecStart=-/usr/bin/podman $LOGGING start --all --filter should-start-on-boot=true
ExecStop=
KillMode=none
EOF
systemctl --user daemon-reload && systemctl --user reset-failed podman-restart.service
```

Why each line: Fedora-family images ship `TimeoutStopFailureMode=abort`, so
shutdown SIGABRTs the service cgroup — killing conmon/rootlessport mid-restore.
Result: containers up, ports dead. `KillMode=none` stops that storm; `infinity`
stops systemd from killing a slow legitimate restore; `ExecStart=` clears the
original before re-adding it with `-` (failures don't fail the unit).

## Autostart script (the logon chain)

Windows: Startup-folder `.vbs` (hidden) → `.bat` → shell script. The script:

1. wait for the container API to answer (`podman info`, retry loop);
2. start the DB container;
3. `podman start --all --filter should-start-on-boot=true` — **twice** (first
   pass can fail on veth teardown / a port held by a dying forwarder);
4. run the port healer (below);
5. run the main start script (health-gated proxy + local servers).

Log everything to one file; a healthy run is quiet, a broken one is diagnosable.

## Port healing

`python heal_container_ports.py` (repo `scripts/`) — probes every published port
of every running container, and for dead ones only: `stop -t 15` + `start`.
Wire it into BOTH the autostart script and the watchdog's second-line recovery
(start-bridge retry → if still down → heal ports → retry once).

## Watchdog

Every few hours: health-check every service; if any is down → run start script;
still down → heal ports → retry; alert only if still down after that. Silences
when healthy. On Windows, scheduled scripts must be **`.py`** (a `.sh` cron job
runs under the WSL bash and mangles Windows paths → exit 127).

## Recovery playbook

1. Run the start script (idempotent).
2. Proxy up, `db:disconnected` → restart the proxy once.
3. Containers running, ports dead → `heal_container_ports.py`.
4. Runtime API hung (`podman ps` never returns) → `podman machine stop` +
   `start`; boot restore brings everything back.
5. Read logs in order: autostart → proxy → the specific local server.

## Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Containers "running", ports dead after reboot | systemd abort-killed restore | Hardened drop-in + healer |
| `podman ps` hangs forever | Wedged client/VM API | `machine stop` + `start` |
| Port "in use", no visible listener | Orphaned rootlessport in the VM netns | Recreate container on a fresh port, SAME volume |
| Scheduled `.sh` dies, `C:UsersUser…` in error | WSL bash ate the path | Make it a `.py` job |
| "no job control in this shell" | Non-interactive backgrounding noise | Expected; verify via logs/health |

## Verification checklist

- [ ] Rehearsed a full stop/start: runtime stopped + bridge killed + autostart run
      → all services and one real completion back
- [ ] `heal_container_ports.py` finds zero dead ports in steady state
- [ ] Boot-restore service: `enabled`, `KillMode=none`, `TimeoutStartUSec=infinity`
- [ ] Watchdog triggered manually once and passed silently
- [ ] Every service has a health endpoint and its own log
