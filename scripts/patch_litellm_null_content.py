#!/usr/bin/env python3
"""Idempotent LiteLLM patch: make replayed reasoning-only messages Ollama-safe.

Problem (verified on LiteLLM 1.101.0, 2026-09-23)
-------------------------------------------------
The Responses API accepts prior-turn ``reasoning`` input items. LiteLLM converts
them to chat-completions messages shaped like::

    {"role": "assistant", "content": None, "reasoning_content": "..."}

Ollama's OpenAI-compatible endpoint rejects an assistant message whose content is
null when there are no tool calls::

    invalid message content type: <nil>

That makes Codex sessions containing reasoning history fail on ``ollama/*`` lanes,
often as repeated "Reconnecting..." followed by HTTP 400. A minimal matrix proved:

======================================  =====
assistant content + reasoning + no tools
``None`` + reasoning + no tools          400
``""`` + reasoning + no tools             200
``None`` + reasoning + tool calls         200
======================================  =====

Fix
---
Coerce only reasoning-only assistant messages to ``content=""`` at the end of
``transform_responses_api_input_to_messages``. Empty content is accepted by both
Ollama and the other chat-completions lanes; tool-call messages are left alone.

The patch is idempotent, compile-checked, backed up, and designed for automatic
re-application from the bridge start script after a LiteLLM upgrade.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import sysconfig
from pathlib import Path

MARKER = "_llmrouter_coerce_null_assistant_content"
RELATIVE_TARGET = Path("litellm/responses/litellm_completion_transformation/transformation.py")

HELPER = '''    @staticmethod
    def _llmrouter_coerce_null_assistant_content(
        messages: list,
    ) -> list:
        """Make replayed reasoning-only assistant messages provider-safe.

        Ollama's OpenAI-compatible endpoint rejects assistant messages with
        content=None when they have no tool_calls ("invalid message content
        type: <nil>"). The Responses-to-chat bridge emits exactly that shape
        for prior reasoning items. Empty content is accepted across chat lanes.
        """
        for _msg in messages:
            if (
                isinstance(_msg, dict)
                and _msg.get("role") == "assistant"
                and _msg.get("content") is None
                and not _msg.get("tool_calls")
            ):
                _msg["content"] = ""
        return messages

'''

OLD_RETURN = (
    "        if not replay_reasoning:\n"
    "            return messages\n"
    "        return LiteLLMCompletionResponsesConfig._merge_reasoning_only_assistant_messages(messages)"
)
NEW_RETURN = (
    "        if not replay_reasoning:\n"
    "            return LiteLLMCompletionResponsesConfig._llmrouter_coerce_null_assistant_content(messages)\n"
    "        return LiteLLMCompletionResponsesConfig._llmrouter_coerce_null_assistant_content(\n"
    "            LiteLLMCompletionResponsesConfig._merge_reasoning_only_assistant_messages(messages)\n"
    "        )"
)
ANCHOR = "    @staticmethod\n    def _merge_reasoning_only_assistant_messages("


def installed_target() -> Path:
    """Resolve the active interpreter's LiteLLM package (portable, no user paths)."""
    return Path(sysconfig.get_paths()["purelib"]) / RELATIVE_TARGET


def patch_text(source: str) -> tuple[str, bool]:
    """Return patched text and whether a change was made."""
    if MARKER in source:
        return source, False
    if OLD_RETURN not in source:
        raise RuntimeError("target return block not found; LiteLLM layout may have changed")
    if ANCHOR not in source:
        raise RuntimeError("helper insertion anchor not found; LiteLLM layout may have changed")
    patched = source.replace(ANCHOR, HELPER + ANCHOR, 1)
    patched = patched.replace(OLD_RETURN, NEW_RETURN, 1)
    compile(patched, str(installed_target()), "exec")
    return patched, True


def apply_patch(target: Path) -> int:
    if not target.is_file():
        print(f"ABORT: target not found: {target}")
        return 1
    source = target.read_text(encoding="utf-8")
    backup = target.with_name(target.name + ".bak-llmrouter-null-content")
    try:
        patched, changed = patch_text(source)
    except RuntimeError as exc:
        print(f"ABORT: {exc}")
        return 1
    if not changed:
        print("already patched — nothing to do")
        return 0
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")
    print("PATCHED: replayed reasoning-only messages will use empty string content")
    return 0


def revert_patch(target: Path) -> int:
    backup = target.with_name(target.name + ".bak-llmrouter-null-content")
    if not backup.is_file():
        print("ABORT: no patch backup found")
        return 1
    shutil.copy2(backup, target)
    print("REVERTED: restored pristine transformation.py")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revert", action="store_true")
    parser.add_argument("--target", type=Path, help="override transformation.py path")
    args = parser.parse_args()
    target = args.target.resolve() if args.target else installed_target()
    return revert_patch(target) if args.revert else apply_patch(target)


if __name__ == "__main__":
    sys.exit(main())
