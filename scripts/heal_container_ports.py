#!/usr/bin/env python3
"""Port-forward healer for container runtimes (Podman/Docker, incl. VM-based).

WHY THIS EXISTS
---------------
After a hard stop/start of the container runtime (especially a VM like Podman
machine on Windows/macOS), a container can end up "running" — its process is
alive — while its published host port is DEAD: the port-forwarder process
(rootlessport / docker-proxy) did not come back. A plain `podman start NAME` is
a NO-OP on a running container, so it never heals. The reliable rebuild is:

    stop -t 15 NAME   &&   start NAME

which recreates the forwarder. This script probes every published port of every
running container and applies that fix ONLY to the dead ones.

USAGE
-----
    python heal_container_ports.py                 # scan + heal all
    python heal_container_ports.py --quiet         # only print problems
    python heal_container_ports.py --names a,b,c   # only these containers
    python heal_container_ports.py --runtime docker

Exit code: 0 if nothing dead (or all healed); 1 if any port is still dead.
"""
from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import time

PROBE_TIMEOUT = 1.5
STOP_WAIT = 15  # seconds before SIGKILL on stop


def run(runtime: str, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([runtime] + args, capture_output=True, text=True, timeout=timeout)


def probe(ip: str, port: int) -> bool:
    target = "127.0.0.1" if ip in ("0.0.0.0", "", "::") else ip
    s = socket.socket()
    s.settimeout(PROBE_TIMEOUT)
    try:
        return s.connect_ex((target, port)) == 0
    finally:
        s.close()


def list_targets(runtime: str, names_filter: set[str] | None):
    proc = run(runtime, ["ps", "--format", "{{.Names}}|{{.Ports}}"])
    if proc.returncode != 0:
        print(f"heal: `${runtime} ps` failed: {proc.stderr.strip()[:200]}")
        return []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, ports = parts[0].strip(), parts[1].strip()
        if not name or name == "NAMES":
            continue
        if names_filter and name not in names_filter:
            continue
        bindings = [(ip, int(hp)) for ip, hp in
                    re.findall(r"(\d+\.\d+\.\d+\.\d+):(\d+)->", ports)]
        if bindings:
            out.append((name, bindings))
    return out


def heal(runtime: str, name: str, bindings) -> bool:
    print(f"heal: {name} — dead forwards {bindings}; stop+start ...")
    try:
        run(runtime, ["stop", "-t", str(STOP_WAIT), name], timeout=STOP_WAIT + 60)
        time.sleep(1)
        run(runtime, ["start", name], timeout=120)
    except subprocess.TimeoutExpired:
        print(f"heal: {name} — stop/start timed out")
        return False
    for _ in range(20):
        if all(probe(ip, port) for ip, port in bindings):
            print(f"heal: {name} — OK")
            return True
        time.sleep(1.5)
    still = [b for b in bindings if not probe(*b)]
    print(f"heal: {name} — STILL DEAD {still}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default="podman", help="podman or docker")
    ap.add_argument("--names", default=None, help="comma-separated container names")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    names_filter = ({n.strip() for n in args.names.split(",")} if args.names else None)
    targets = list_targets(args.runtime, names_filter)

    dead = [(n, [b for b in bs if not probe(*b)]) for n, bs in targets]
    dead = [(n, bs) for n, bs in dead if bs]

    if not dead:
        if not args.quiet:
            print(f"heal: all {len(targets)} container port sets OK")
        return 0

    if not args.quiet:
        print(f"heal: {len(dead)} container(s) with dead forwards of {len(targets)}")
    failed = sum(0 if heal(args.runtime, n, bs) else 1 for n, bs in dead)
    if failed:
        print(f"heal: {failed} container(s) still broken after heal")
        return 1
    if not args.quiet:
        print("heal: all repaired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
