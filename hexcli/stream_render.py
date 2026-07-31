#!/usr/bin/env python3
"""hexcli.stream_render — incremental rendering of a streaming agent response.

At ~15 tok/s a 300-token answer takes 20+ seconds. v1.7 streamed nothing to
the user: it showed a token counter and printed the finished text at the end,
so every turn looked like a hang (review finding W6). But the agent protocol
emits JSON actions, and dumping raw JSON at the user is worse than a spinner.

This module resolves that: it consumes streamed deltas and decides what a
human should see, without waiting for the response to finish.

  * `{"action":"finish","message":"..."}` → the MESSAGE TEXT streams live,
    JSON escapes decoded, quotes/braces never shown.
  * `{"action":"read_file",...}` → announce the tool as soon as the action
    name is complete ("→ read_file"), then stay quiet.
  * Plain prose (no JSON) → stream as-is.

Pure state machine: no I/O, no globals. The caller supplies an emit callback,
which makes it fully testable and lets the REPL, evals, and future UIs share
one implementation.
"""
from __future__ import annotations

from collections.abc import Callable

_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r", "/": "/", "b": "\b", "f": "\f"}


class StreamRenderer:
    """Feed it deltas; it emits only what a human should read.

    States:
      probing  — undecided: JSON action or prose?
      prose    — not JSON; everything passes through
      scanning — JSON: watching for "action" / "message" values
      message  — inside the message string; decoded chars stream out
      done     — message closed; ignore the rest (trailing JSON)
    """

    def __init__(self, emit: Callable[[str], None],
                 on_tool: Callable[[str], None] | None = None) -> None:
        self._emit = emit
        self._on_tool = on_tool
        self._state = "probing"
        self._buf = ""            # raw text seen so far (probing/scanning)
        self._pending_esc = False
        self._unicode: str | None = None
        self._announced = False
        self.tool_announced: str | None = None
        self.text_emitted = ""

    def _announce(self, name: str) -> None:
        self.tool_announced = name
        self._announced = True
        if self._on_tool:
            self._on_tool(name)

    # -- public API ---------------------------------------------------------

    def feed(self, delta: str) -> None:
        for ch in delta:
            self._feed_char(ch)

    def finish(self) -> None:
        """Flush anything still buffered (short prose that never resolved)."""
        if self._state == "probing" and self._buf.strip():
            self._emit_text(self._buf)
            self._state = "prose"
            self._buf = ""

    # -- internals ----------------------------------------------------------

    def _emit_text(self, text: str) -> None:
        if text:
            self.text_emitted += text
            self._emit(text)

    def _feed_char(self, ch: str) -> None:
        st = self._state
        if st == "prose":
            self._emit_text(ch)
            return
        if st == "done":
            return
        if st == "message":
            self._feed_message_char(ch)
            return

        # probing / scanning
        self._buf += ch
        if st == "probing":
            stripped = self._buf.lstrip()
            if not stripped:
                return
            # A leading '{' means JSON. So does a ``` fence whose body has
            # started with '{' — models routinely wrap the action in a fence.
            if stripped[0] == "{":
                self._state = "scanning"
            elif stripped.startswith("`"):
                after_fence = stripped.lstrip("`")
                # Skip an optional language tag, then the newline.
                nl = after_fence.find("\n")
                if nl != -1:
                    body = after_fence[nl + 1:].lstrip()
                    if body.startswith("{"):
                        self._state = "scanning"
                        self._buf = body
                    elif body:
                        self._state = "prose"
                        self._emit_text(self._buf)
                        self._buf = ""
                return  # still ambiguous until the fence line completes
            else:
                # Definitely prose: release everything buffered so far.
                self._state = "prose"
                self._emit_text(self._buf)
                self._buf = ""
                return

        if self._state == "scanning":
            self._scan()

    def _scan(self) -> None:
        """Look for a completed "action" value or the start of "message"."""
        if not self._announced:
            act = _completed_string_value(self._buf, "action")
            if act == "tool":
                # v1's nested form {"action":"tool","tool":"read_file"} — the
                # real name arrives in a second field; wait for it rather than
                # announcing the placeholder (and re-announcing every char).
                name = _completed_string_value(self._buf, "tool")
                if name:
                    self._announce(name)
            elif act:
                self.tool_announced = act
                self._announced = True
                if act != "finish" and self._on_tool:
                    self._on_tool(act)

        marker = _message_value_start(self._buf)
        if marker is not None:
            rest = self._buf[marker:]
            self._state = "message"
            self._buf = ""
            for ch in rest:
                self._feed_message_char(ch)

    def _feed_message_char(self, ch: str) -> None:
        if self._unicode is not None:
            self._unicode += ch
            if len(self._unicode) == 4:
                try:
                    self._emit_text(chr(int(self._unicode, 16)))
                except ValueError:
                    pass
                self._unicode = None
            return
        if self._pending_esc:
            self._pending_esc = False
            if ch == "u":
                self._unicode = ""
            else:
                self._emit_text(_ESCAPES.get(ch, ch))
            return
        if ch == "\\":
            self._pending_esc = True
            return
        if ch == '"':
            self._state = "done"
            return
        self._emit_text(ch)


# ---------------------------------------------------------------------------
# Helpers — string scanning that respects JSON escaping
# ---------------------------------------------------------------------------

def _completed_string_value(buf: str, key: str) -> str | None:
    """Return the value of "key": "value" once its closing quote has arrived."""
    needle = f'"{key}"'
    i = buf.find(needle)
    if i == -1:
        return None
    j = buf.find(":", i + len(needle))
    if j == -1:
        return None
    k = buf.find('"', j + 1)
    if k == -1:
        return None
    end = _closing_quote(buf, k + 1)
    if end == -1:
        return None
    return buf[k + 1:end]


def _message_value_start(buf: str) -> int | None:
    """Index just past the opening quote of "message": "…", if present."""
    needle = '"message"'
    i = buf.find(needle)
    if i == -1:
        return None
    j = buf.find(":", i + len(needle))
    if j == -1:
        return None
    k = buf.find('"', j + 1)
    if k == -1:
        return None
    return k + 1


def _closing_quote(buf: str, start: int) -> int:
    """Index of the unescaped closing quote at/after `start`, or -1."""
    esc = False
    for idx in range(start, len(buf)):
        ch = buf[idx]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            return idx
    return -1
