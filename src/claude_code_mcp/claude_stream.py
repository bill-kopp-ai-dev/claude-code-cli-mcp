from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedStreamEvent:
    type: str          # "system","user","assistant","tool_use","tool_result","result","error","unknown"
    raw: dict[str, Any]  # full parsed JSON
    text_delta: str | None = None  # for "stream_event" partials with text deltas


class ClaudeStreamParser:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.result_event: dict[str, Any] | None = None
        self.partial_text: str = ""
        self.errors: list[str] = []

    def feed_line(self, line: str) -> ParsedStreamEvent | None:
        """Parse one NDJSON line. Return None on blank/incomplete lines.

        - If the line is empty or whitespace-only, return None.
        - If JSON parse fails, append to self.errors and return a
          ParsedStreamEvent(type="unknown", raw={}).
        - Otherwise, classify by top-level "type" and update internal state.
        """
        line = line.strip()
        if not line:
            return None

        import json
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            self.errors.append(f"JSONDecodeError: {e} for line: {line!r}")
            return ParsedStreamEvent(type="unknown", raw={})

        if not isinstance(raw, dict):
            # If the NDJSON line contains a JSON array or literal instead of a dict
            return ParsedStreamEvent(type="unknown", raw={})

        raw_type = raw.get("type")

        # Classify the event type
        if raw_type in ("system", "user", "assistant", "tool_use", "tool_result", "result", "error"):
            evt_type = raw_type
        elif raw_type == "stream_event":
            evt_type = "assistant"
        else:
            evt_type = "unknown"

        text_delta: str | None = None
        if raw_type == "stream_event":
            # Extract delta.text when present (checking nested structures safely)
            delta = raw.get("delta")
            if not isinstance(delta, dict) and "event" in raw and isinstance(raw["event"], dict):
                delta = raw["event"].get("delta")

            if isinstance(delta, dict):
                text_delta = delta.get("text")
                if text_delta:
                    self.partial_text += text_delta

        # Accumulate appropriate events in messages and result_event
        if raw_type == "result":
            self.result_event = raw
            self.messages.append(raw)
        elif raw_type in ("system", "user", "assistant", "tool_use", "tool_result"):
            self.messages.append(raw)

        return ParsedStreamEvent(type=evt_type, raw=raw, text_delta=text_delta)


class RollingLineBuffer:
    """Helper to accumulate raw stream text chunks and split them into complete lines."""

    def __init__(self) -> None:
        self._buffer: list[str] = []

    def feed(self, chunk: str) -> list[str]:
        """Feed a string chunk, return a list of complete lines."""
        if not chunk:
            return []
        self._buffer.append(chunk)
        combined = "".join(self._buffer)
        if "\n" not in combined:
            self._buffer = [combined]
            return []
        lines = combined.split("\n")
        # Keep the last incomplete part in the buffer
        self._buffer = [lines.pop()]
        return [line for line in lines if line]

    def get_remaining(self) -> str:
        return "".join(self._buffer)
