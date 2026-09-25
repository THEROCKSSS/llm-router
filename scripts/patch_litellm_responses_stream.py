"""patch_litellm_responses_stream.py — backport upstream's message-item opener fix.

WHY: LiteLLM's chat->Responses bridge (used by Codex via /v1/responses) opens a
REASONING output item when a lane streams reasoning first, then streams
`response.output_text.delta` for the answer WITHOUT ever opening a message item
for it. Strict clients (Codex) log:

    ERROR codex_core::util: OutputTextDelta without active item

...and in the worst case drop the response from the TUI. The bug is fixed on
LiteLLM upstream MAIN (separate `sent_message_item_added_event` flag +
`_queue_message_item_added_events()` helper) but NOT in any released version as
of 1.102.0 (checked). This script surgically applies that fix to the installed
package.

RERUN AFTER EVERY `uv tool upgrade litellm` / litellm reinstall — upgrades
overwrite the file. The script is idempotent: it detects an already-patched file
and exits cleanly. Remove this patch once a litellm release ships the fix
(check for `sent_message_item_added_event` in the installed streaming_iterator).

Usage:  python patch_litellm_responses_stream.py [--revert]
"""

import argparse
import re
import shutil
import sys
import sysconfig
from pathlib import Path

TARGET = Path(
    sysconfig.get_paths()["purelib"]
) / "litellm" / "responses" / "litellm_completion_transformation" / "streaming_iterator.py"

# ---------------------------------------------------------------- helpers

def apply(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"ABORT [{label}]: expected 1 occurrence, found {n}")
    return text.replace(old, new)


# ---------------------------------------------------------------- patches

PATCH_INIT = (
    "        self.sent_output_item_added_event: bool = False\n"
    "        self.sent_content_part_added_event: bool = False\n",
    "        self.sent_output_item_added_event: bool = False\n"
    "        self.sent_message_item_added_event: bool = False\n"
    "        self._message_output_index: int = 0\n"
    "        self.sent_content_part_added_event: bool = False\n",
)

PATCH_QUEUE_HELPER = (
    "    def create_content_part_added_event(self) -> ContentPartAddedEvent:\n",
    '''    def _queue_message_item_added_events(self) -> None:
        """Open a message item for the answer text (upstream fix, PR backport).

        Needed because the reasoning item opens `sent_output_item_added_event`
        first, so the text item never got its own `output_item.added` - strict
        clients (Codex) then reject the text deltas.
        """
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"
        self.sent_message_item_added_event = True
        self.sent_content_part_added_event = True
        if self._cached_reasoning_item_id is not None:
            self._message_output_index = self._next_tool_output_index
            self._next_tool_output_index += 1
        else:
            self._message_output_index = 0
        self._sequence_number += 1
        event: Final = OutputItemAddedEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
            output_index=self._message_output_index,
            item=BaseLiteLLMOpenAIResponseObject(
                **{
                    "id": self._cached_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
            ),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        self._pending_response_events.append(event)
        self._pending_response_events.append(self.create_content_part_added_event())

    def create_content_part_added_event(self) -> ContentPartAddedEvent:\n''',
)

PATCH_ENSURE = (
    """        # Change: Never return a value, just enqueue output item events
        if self.sent_output_item_added_event:
            return
        delta: Final = chunk.choices[0].delta

        self._sequence_number += 1
        self.sent_output_item_added_event = True
""",
    """        # Change: Never return a value, just enqueue output item events
        if self.sent_output_item_added_event:
            return
        if not chunk.choices:
            return
        delta: Final = chunk.choices[0].delta

        self._sequence_number += 1
        self.sent_output_item_added_event = True
""",
)

PATCH_ENSURE_DEFAULT = (
    """        # Default: message
        self._cached_item_id = self._cached_item_id or f"msg_{uuid.uuid4()}"
        event = OutputItemAddedEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
            output_index=0,
            item=BaseLiteLLMOpenAIResponseObject(
                **{
                    "id": self._cached_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
            ),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        self._pending_response_events.append(event)

        # Emit content_part.added immediately after output_item.added for message
        # items. The OpenAI Responses spec requires this event before any
        # output_text.delta events so downstream parsers can initialize the
        # text part structure.
        if not self.sent_content_part_added_event:
            self.sent_content_part_added_event = True
            content_part_event: Final = self.create_content_part_added_event()
            self._pending_response_events.append(content_part_event)
        return
""",
    """        # Default: message
        self._queue_message_item_added_events()
        return
""",
)

PATCH_TEXT_DELTA = (
    """        if delta_content:
            self._sequence_number += 1
            text_delta_event: Final = OutputTextDeltaEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
                item_id=item_id,
                output_index=0,
                content_index=0,
                delta=delta_content,
            )
""",
    """        if delta_content:
            if not self.sent_message_item_added_event:
                self._queue_message_item_added_events()
            self._sequence_number += 1
            text_delta_event: Final = OutputTextDeltaEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
                item_id=item_id,
                output_index=self._message_output_index,
                content_index=0,
                delta=delta_content,
            )
""",
)

PATCH_CONTENT_PART_ADDED_IDX = (
    """        event: Final = ContentPartAddedEvent(
            type=ResponsesAPIStreamEvents.CONTENT_PART_ADDED,
            item_id=self._cached_item_id,
            output_index=0,
            content_index=0,
""",
    """        event: Final = ContentPartAddedEvent(
            type=ResponsesAPIStreamEvents.CONTENT_PART_ADDED,
            item_id=self._cached_item_id,
            output_index=self._message_output_index,
            content_index=0,
""",
)

PATCH_TEXT_DONE_IDX = (
    """        return OutputTextDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            item_id=self._cached_item_id,
            output_index=0,
            content_index=0,
""",
    """        return OutputTextDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            item_id=self._cached_item_id,
            output_index=self._message_output_index,
            content_index=0,
""",
)

PATCH_PART_DONE_IDX = (
    """        return ContentPartDoneEvent(
            type=ResponsesAPIStreamEvents.CONTENT_PART_DONE,
            item_id=self._cached_item_id,
            output_index=0,
            content_index=0,
""",
    """        return ContentPartDoneEvent(
            type=ResponsesAPIStreamEvents.CONTENT_PART_DONE,
            item_id=self._cached_item_id,
            output_index=self._message_output_index,
            content_index=0,
""",
)

PATCH_ITEM_DONE_IDX = (
    """        return OutputItemDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
            output_index=0,
            sequence_number=1,
""",
    """        return OutputItemDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
            output_index=self._message_output_index,
            sequence_number=1,
""",
)

PATCH_DONE_EVENTS = (
    """    def return_default_done_events(
        self, litellm_complete_object: ModelResponse
    ) -> BaseLiteLLMOpenAIResponseObject | None:
        if self.sent_output_text_done_event is False:
""",
    """    def return_default_done_events(
        self, litellm_complete_object: ModelResponse
    ) -> BaseLiteLLMOpenAIResponseObject | None:
        if self.sent_message_item_added_event is False:
            final_content: Final = litellm_complete_object.choices[0].message.content or ""
            if not final_content:
                self.sent_output_text_done_event = True
                self.sent_output_content_part_done_event = True
                self.sent_output_item_done_event = True
                return None
            self._queue_message_item_added_events()
            return self._pending_response_events.pop(0)
        if self.sent_output_text_done_event is False:
""",
)

# Sync __next__: drop the pre-transform pending return; queue + pop at the tail
# (mirrors upstream main; also preserves the first delta of a queued chunk).
PATCH_NEXT_SYNC = (
    """                    self.collected_chat_completion_chunks.append(
                        self._snapshot_chunk_for_stream_chunk_builder(cast(ModelResponseStream, chunk))
                    )
                    # Emit any just-queued output_item event
                    if self._pending_response_events:
                        return self._pending_response_events.pop(0)
                    response_api_chunk = self._transform_chat_completion_chunk_to_response_api_chunk(chunk)
                    if response_api_chunk:
                        return response_api_chunk
""",
    """                    self.collected_chat_completion_chunks.append(
                        self._snapshot_chunk_for_stream_chunk_builder(cast(ModelResponseStream, chunk))
                    )
                    response_api_chunk = self._transform_chat_completion_chunk_to_response_api_chunk(chunk)
                    if response_api_chunk:
                        self._pending_response_events.append(response_api_chunk)
                    if self._pending_response_events:
                        return self._pending_response_events.pop(0)
""",
)

ALL_PATCHES = [
    ("init flags", PATCH_INIT),
    ("queue helper", PATCH_QUEUE_HELPER),
    ("ensure guards", PATCH_ENSURE),
    ("ensure default", PATCH_ENSURE_DEFAULT),
    ("text delta opener", PATCH_TEXT_DELTA),
    ("content_part.added idx", PATCH_CONTENT_PART_ADDED_IDX),
    ("output_text.done idx", PATCH_TEXT_DONE_IDX),
    ("content_part.done idx", PATCH_PART_DONE_IDX),
    ("output_item.done idx", PATCH_ITEM_DONE_IDX),
    ("done-event opener", PATCH_DONE_EVENTS),
    ("sync __next__ order", PATCH_NEXT_SYNC),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true",
                    help="restore the pristine file from the .orig backup")
    args = ap.parse_args()

    if not TARGET.is_file():
        print(f"ERROR: not found: {TARGET}")
        return 2

    text = TARGET.read_text(encoding="utf-8")
    backup = TARGET.with_suffix(".py.orig-streamfix")

    if args.revert:
        if backup.is_file():
            shutil.copy2(backup, TARGET)
            print(f"reverted from {backup}")
            return 0
        print("nothing to revert (no backup)")
        return 1

    if "sent_message_item_added_event" in text:
        print("already patched - nothing to do (idempotent).")
        return 0

    if not backup.is_file():
        shutil.copy2(TARGET, backup)
        print(f"backup written: {backup}")

    for label, (old, new) in ALL_PATCHES:
        text = apply(text, old, new, label)
        print(f"  + {label}")

    # syntax check before writing
    compile(text, str(TARGET), "exec")

    TARGET.write_text(text, encoding="utf-8")
    print(f"\nPATCHED: {TARGET}")
    print("Restart the LiteLLM proxy to load it, then:")
    print("  curl -sN .../v1/responses -d '{\"model\":\"ollama/deepseek-v4.1-flash\",...}'")
    print("  -> expect response.output_item.added (message) BEFORE output_text.delta")
    return 0


if __name__ == "__main__":
    sys.exit(main())
