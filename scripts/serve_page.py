#!/usr/bin/env python3
"""Serve a static page (or a whole folder) over localhost HTTP — no framework.

WHY THIS EXISTS
---------------
A reference page, dashboard, command cheat-sheet, or built report is far more
useful when it has a stable URL than when it's a file you hunt for on disk.
This is a ~60-line stdlib server that:

  * serves one file for every path (or a directory, if given one);
  * has a /health endpoint (so autostart/watchdog scripts can gate on it);
  * exits quietly when the port is already taken — safe for idempotent
    autostart scripts that may run twice;
  * binds 127.0.0.1 so nothing is exposed to the network by default.

PAIR WITH A REVERSE PROXY (optional)
------------------------------------
To reach it from other devices, put any TLS-capable reverse proxy in front
(Tailscale Serve, Caddy, nginx). Example with a reverse proxy that maps a
path prefix onto a local port:

    python serve_page.py --root ./site --port 8901
    <proxy>: map  /ref  ->  http://127.0.0.1:8901
    # → https://<your-host>/ref  becomes reachable from your devices

Note: some proxy tools refuse to serve a raw FILE PATH directly without admin
rights (e.g. Tailscale Serve on Windows); proxying to a local HTTP server
(this script) needs no elevation.

USAGE
    python serve_page.py                       # serves ./index.html on :8901
    python serve_page.py --root path/to/page.html
    python serve_page.py --root path/to/dir/ --port 9000
    python serve_page.py --root report.html --port 8080 --host 127.0.0.1

Env vars: SERVE_ROOT, SERVE_PORT, SERVE_HOST
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_ROOT = os.environ.get("SERVE_ROOT", "./index.html")
DEFAULT_PORT = int(os.environ.get("SERVE_PORT", "8901"))
DEFAULT_HOST = os.environ.get("SERVE_HOST", "127.0.0.1")

ROOT: Path  # set in main()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet; health checks would otherwise flood the log

    def _send(self, body: bytes, ctype: str, code: int = 200, head_only: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _resolve(self) -> Path | None:
        """Map the request path to a file under ROOT.

        - ROOT is a file  -> every path serves that file.
        - ROOT is a dir   -> serve the matching file inside it (index.html at /).
        """
        if ROOT.is_file():
            return ROOT
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        candidate = (ROOT / rel).resolve()
        # prevent path traversal outside ROOT
        try:
            candidate.relative_to(ROOT.resolve())
        except ValueError:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    def _ctype(self, path: Path) -> str:
        guess, _ = mimetypes.guess_type(str(path))
        if guess is None:
            guess = "application/octet-stream"
        if guess.startswith("text/") or guess == "application/json":
            guess += "; charset=utf-8"
        return guess

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send(b"ok", "text/plain; charset=utf-8")
            return
        target = self._resolve()
        if target is None:
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        try:
            self._send(target.read_bytes(), self._ctype(target))
        except OSError as e:
            self._send(f"read error: {e}".encode(), "text/plain; charset=utf-8", 500)

    def do_HEAD(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send(b"", "text/plain; charset=utf-8", head_only=True)
            return
        target = self._resolve()
        if target is None:
            self._send(b"", "text/plain; charset=utf-8", 404, head_only=True)
            return
        self._send(b"", self._ctype(target), head_only=True)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex((host, port)) == 0


def main() -> int:
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="file to serve, or a directory (default: %(default)s)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST)
    args = ap.parse_args()

    ROOT = Path(args.root).expanduser()
    if not ROOT.exists():
        print(f"serve: root not found: {ROOT}", file=sys.stderr)
        return 2

    if port_in_use(args.host, args.port):
        # Already serving — autostart scripts may run this twice; not an error.
        print(f"serve: {args.host}:{args.port} already in use; exiting quietly")
        return 0

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    kind = "file" if ROOT.is_file() else "dir"
    print(f"serve: {kind} {ROOT} → http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
