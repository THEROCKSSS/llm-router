#!/usr/bin/env bash
# ============================================================================
# start-bridge.sh — idempotent launcher for a multi-lane LLM router stack.
#
# Safe to run any time (every step is health-gated: it starts only what is
# down). Order: DB container -> router proxy -> local model servers.
#
# ADAPT: set ROUTER_DIR / LOCAL_SERVERS below to your layout. Secrets are read
# from .env (see .env.example) — never hardcode keys here.
#
# Usage:   bash start-bridge.sh
# Requires: litellm on PATH (or set LITELLM_BIN), podman/docker for the DB.
# ============================================================================
set -uo pipefail

# ---- EDIT: your layout ------------------------------------------------------
ROUTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${ROUTER_CONFIG:-$ROUTER_DIR/litellm.yaml}"
PROXY_PORT="${PROXY_PORT:-4000}"
DB_CONTAINER="${DB_CONTAINER:-litellm-db}"
DB_PORT="${DB_PORT:-5434}"
LITELLM_BIN="${LITELLM_BIN:-litellm}"
# The package patches must run with the SAME interpreter that owns the installed
# LiteLLM package. For uv-tool installs that is usually python(.exe) beside the
# litellm entry point. Override PYTHON_BIN explicitly for other layouts.
if [ -z "${PYTHON_BIN:-}" ]; then
  BIN_DIR="$(dirname "$LITELLM_BIN")"
  if   [ -x "$BIN_DIR/python.exe" ]; then PYTHON_BIN="$BIN_DIR/python.exe"
  elif [ -x "$BIN_DIR/python" ];   then PYTHON_BIN="$BIN_DIR/python"
  else PYTHON_BIN="$(command -v python3 || command -v python || true)"
  fi
fi
# Local model servers to bring up: "name:port:start-command" (one per line).
LOCAL_SERVERS=(
  # "laya:4030:python laya_server.py"
)
declare -A LOCAL_SERVER_READY=(
  # [laya]="http://127.0.0.1:4030/info"
)
declare -A LOCAL_SERVER_READY_PATTERN=(
  # [laya]='"laya_version"'
)
# -----------------------------------------------------------------------------

log() { echo "[bridge] $*"; }

# 0) load secrets (.env) if present — never echo values
if [ -f "$ROUTER_DIR/.env" ]; then
  set -a; . "$ROUTER_DIR/.env"; set +a
fi

# 1) DB container (harmless no-op if already up)
if ! (echo > "/dev/tcp/127.0.0.1/$DB_PORT") 2>/dev/null; then
  log "starting DB container ($DB_CONTAINER) ..."
  (podman start "$DB_CONTAINER" 2>/dev/null || docker start "$DB_CONTAINER" 2>/dev/null) || true
  for _ in $(seq 1 30); do
    (echo > "/dev/tcp/127.0.0.1/$DB_PORT") 2>/dev/null && break
    sleep 1
  done
fi

# 2) Self-healing LiteLLM package patches (safe to omit)
# Upgrades overwrite installed package files. Each script is idempotent, backs up
# the original, compile-checks its result, and no-ops when already applied.
if [ -n "$PYTHON_BIN" ]; then
  for patch_script in \
      patch_litellm_responses_stream.py \
      patch_litellm_null_content.py; do
    if [ -f "$ROUTER_DIR/$patch_script" ]; then
      log "applying $patch_script"
      "$PYTHON_BIN" "$ROUTER_DIR/$patch_script" || \
        log "WARN: $patch_script failed; see output above"
    fi
  done
fi

# 3) Local model servers (start before the proxy so lanes resolve)
for entry in "${LOCAL_SERVERS[@]:-}"; do
  [ -z "$entry" ] && continue
  name="${entry%%:*}"; rest="${entry#*:}"; port="${rest%%:*}"; cmd="${rest#*:}"
  if ! curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/health"; then
    log "starting local server '$name' on :$port ..."
    (cd "$ROUTER_DIR" && nohup bash -c "$cmd" >> "$name.log" 2>&1 &)
  fi
  # A server may bind its port before its GPU checkpoints are ready, so
  # /health is not sufficient. LOCAL_SERVER_READY[name] is a readiness URL
  # (default /health); Laya overrides it with /info + a content grep.
  ready_url="${LOCAL_SERVER_READY[$name]:-http://127.0.0.1:$port/health}"
  ready_pat="${LOCAL_SERVER_READY_PATTERN[$name]:-.}"
  for _ in $(seq 1 600); do   # cold preloads can take minutes
    curl -s -m 2 "$ready_url" 2>/dev/null | grep -q "$ready_pat" && break
    sleep 1
  done
  done

# 4) The router proxy
if ! curl -s -o /dev/null -m 2 "http://127.0.0.1:$PROXY_PORT/health/liveliness"; then
  log "starting LiteLLM proxy on :$PROXY_PORT ..."
  # PYTHONIOENCODING=utf-8: without it, a cp1252 Windows console makes LiteLLM's
  # startup banner raise UnicodeEncodeError inside show_banner() and the process
  # dies before it ever listens ("Application startup failed. Exiting.").
  (cd "$ROUTER_DIR" && PYTHONIOENCODING=utf-8 nohup "$LITELLM_BIN" --config "$CONFIG" \
      --host 127.0.0.1 --port "$PROXY_PORT" >> proxy.log 2>&1 &)
  for _ in $(seq 1 240); do
    curl -s -o /dev/null -m 1 "http://127.0.0.1:$PROXY_PORT/health/liveliness" && break
    sleep 1
  done
fi

# 5) Status + cold-start DB flake handling
if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PROXY_PORT/health/liveliness"; then
  READY=$(curl -s -m 8 -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-}" \
    "http://127.0.0.1:$PROXY_PORT/health/readiness" 2>/dev/null || true)
  if echo "$READY" | grep -q '"db":"disconnected"'; then
    log "db disconnected on cold start — restarting proxy once ..."
    # kill whatever owns the port, then relaunch
    case "$(uname -s)" in
      MINGW*|MSYS*|CYGWIN*)
        powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort $PROXY_PORT -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" ;;
      *) fuser -k "$PROXY_PORT/tcp" 2>/dev/null || true ;;
    esac
    sleep 3
    (cd "$ROUTER_DIR" && PYTHONIOENCODING=utf-8 nohup "$LITELLM_BIN" --config "$CONFIG" \
        --host 127.0.0.1 --port "$PROXY_PORT" >> proxy.log 2>&1 &)
    for _ in $(seq 1 240); do
      curl -s -o /dev/null -m 1 "http://127.0.0.1:$PROXY_PORT/health/liveliness" && break
      sleep 1
    done
  fi
  log "LIVE — http://127.0.0.1:$PROXY_PORT/v1"
else
  log "FAILED — see proxy.log"
  exit 1
fi
