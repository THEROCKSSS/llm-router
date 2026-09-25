#!/usr/bin/env bash
# Wire a Hermes profile onto the local LLM router (custom provider + fallback).
# Run once, editing PROFILE and the lane names. Docs: docs/04-CLIENTS.md
set -euo pipefail

PROFILE="${1:-myprofile}"
DEFAULT_LANE="${2:-openai-compatible/<your-default-lane>}"
ROUTER="http://127.0.0.1:4000/v1"

hermes -p "$PROFILE" config set providers.router.name "Local LLM Router"
hermes -p "$PROFILE" config set providers.router.base_url "$ROUTER"
hermes -p "$PROFILE" config set providers.router.key_env "LLM_ROUTER_HERMES_KEY"
hermes -p "$PROFILE" config set providers.router.api_mode "chat_completions"
hermes -p "$PROFILE" config set providers.router.default_model "$DEFAULT_LANE"

hermes -p "$PROFILE" config set model.provider "custom:router"
hermes -p "$PROFILE" config set model.default "$DEFAULT_LANE"
hermes -p "$PROFILE" config set model.api_mode "chat_completions"

# Fallback so the agent survives the router being down (edit to a provider
# you have a direct key for).
hermes -p "$PROFILE" config set fallback_providers \
  '[{"provider":"ollama-cloud","model":"deepseek-v4.1-flash"}]'

# Optional quick aliases
hermes -p "$PROFILE" config set model_aliases.decide \
  '{"model":"laya","provider":"custom:router"}' --force

echo "Done. Put LLM_ROUTER_HERMES_KEY=<hermes virtual key> in the profile .env."
echo "Verify: hermes -p $PROFILE chat -q 'Reply with exactly: OK' -Q"
