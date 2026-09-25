@echo off
rem Codex via the local LLM router — Windows wrapper.
rem Place at: %USERPROFILE%\.local\bin\codex-router.bat
rem Requires: LLM_ROUTER_CODEX_KEY set (user env or ~/.codex/.env)
codex --profile router %*
