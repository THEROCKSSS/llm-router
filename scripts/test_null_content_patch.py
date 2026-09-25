#!/usr/bin/env python3
"""Regression test for the Ollama null-content session-resume patch.

The script applies the patch to a temporary copy of a small fixture that mirrors
LiteLLM's real method layout, then verifies the transformation output for three
input shapes. It does not touch the installed LiteLLM package.

Run:
    python scripts/test_null_content_patch.py
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

PATCH_SCRIPT = Path(__file__).with_name("patch_litellm_null_content.py")


def load_patch_module():
    spec = importlib.util.spec_from_file_location("null_content_patch", PATCH_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fixture() -> str:
    """Minimal stand-in exposing the same method transform_responses_api_input_to_messages."""
    return '''class LiteLLMCompletionResponsesConfig:
    @staticmethod
    def _merge_reasoning_only_assistant_messages(messages):
        return messages

    @staticmethod
    def transform_responses_api_input_to_messages(messages, replay_reasoning=True):
        if not replay_reasoning:
            return messages
        return LiteLLMCompletionResponsesConfig._merge_reasoning_only_assistant_messages(messages)
'''


def transform_with_patch(patched: str) -> dict:
    with tempfile.TemporaryDirectory() as td:
        mod_path = Path(td) / "fixture.py"
        mod_path.write_text(patched, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("fixture", mod_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls = mod.LiteLLMCompletionResponsesConfig
        return {
            "null_no_tool": cls.transform_responses_api_input_to_messages(
                [{"role": "assistant", "content": None, "reasoning_content": "t"}]
            ),
            "empty_no_tool": cls.transform_responses_api_input_to_messages(
                [{"role": "assistant", "content": "", "reasoning_content": "t"}]
            ),
            "null_with_tool": cls.transform_responses_api_input_to_messages(
                [{"role": "assistant", "content": None, "reasoning_content": "t",
                  "tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "x", "arguments": "{}"}}]}]
            ),
        }


def main() -> int:
    mod = load_patch_module()
    fixture = make_fixture()
    before = transform_with_patch(fixture)
    assert before["null_no_tool"][0]["content"] is None, "fixture should start unpatched"

    patched, changed = mod.patch_text(fixture)
    assert changed, "patch_text should report a change on unpatched fixture"
    after = transform_with_patch(patched)

    assert after["null_no_tool"][0]["content"] == "", "null + no tool_calls must become ''"
    assert after["empty_no_tool"][0]["content"] == "", "empty content must be unchanged"
    assert after["null_with_tool"][0]["content"] is None, "tool-call message must be untouched"

    _, changed_again = mod.patch_text(patched)
    assert not changed_again, "patch must be idempotent"
    print("OK null-content patch: 3/3 shapes correct, idempotent on reapply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
