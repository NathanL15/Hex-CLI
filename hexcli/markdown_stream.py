#!/usr/bin/env python3
"""hexcli.markdown_stream — markdown-lite to ANSI, one character at a time.

Answers arrive as a token stream and used to print raw: ``## Heading``,
``**bold**``, backtick spans and ``` fences all showed their markers. This
turns the common subset into terminal styling without waiting for the line
to finish, so streaming keeps its word-by-word feel:

* ``# heading`` (any level)      → bold, markers dropped
* ``- item`` / ``* item`` / ``+ item`` → ``• item`` (indentation kept)
* ``**bold**``                   → bold
* `` `code` ``                   → cyan
* ``` ```lang ``` … ``` ``` ```  → a dim rule with the language, code left
  exactly as written (no gutter, so it copies cleanly), a dim rule after

Markers are held only as long as they are ambiguous (at most the three
backticks of a fence, or one ``*``) and released literally when they turn
out not to be markup, so ``2 * 3`` and ``C# code`` come through untouched.
Feeding the same text whole or one character at a time yields identical
output; that property is what the tests pin down.

The styles come from ``ui.C`` and are empty strings when colour is off, in
which case only the bullet and fence substitutions remain.
"""
from __future__ import annotations

_FENCE_WIDTH = 40
_FENCE_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+#.-")
_FENCE_LABEL_MAX = 24   # "```" then more than a language name is not a fence line


class MarkdownStream:
    def __init__(self) -> None:
        from hexcli.ui import C  # lazy: ui imports this module for render_result
        self._c = C
        self._bol = True            # at the beginning of a line
        self._pending = ""          # marker characters not yet decided
        self._fence = False         # inside a ``` block
        self._fence_line: str | None = None   # collecting "```lang" up to its newline
        self._bold = False
        self._code = False
        self._heading = False
        self._restyle = False       # re-emit the open styles after a soft line break

    # -- public -------------------------------------------------------------

    def feed(self, text: str) -> str:
        return "".join(self._char(ch) for ch in text)

    def finish(self) -> str:
        """Release anything still held and close open styling."""
        out = self._flush_pending()
        if self._fence_line is not None:
            out += self._fence_marker(self._fence_line)
            self._fence_line = None
        if self._restyle:
            # Styles carried over a line break were already reset on screen
            # and never re-opened: nothing to close.
            self._bold = self._code = self._heading = False
            self._restyle = False
        return out + self._close_styles()

    # -- styling ------------------------------------------------------------

    def _style(self) -> str:
        c = self._c
        return (c.BOLD if (self._bold or self._heading) else "") + (c.BCYAN if self._code else "")

    def _close_styles(self) -> str:
        if not (self._bold or self._code or self._heading):
            return ""
        self._bold = self._code = self._heading = False
        return self._c.RESET

    def _toggle_bold(self) -> str:
        if self._code:
            return "**"   # literal inside a code span
        self._bold = not self._bold
        return self._c.RESET + self._style() if not self._bold else self._c.BOLD

    def _toggle_code(self) -> str:
        self._code = not self._code
        return (self._c.BCYAN if self._code else self._c.RESET + self._style())

    def _bullet(self) -> str:
        c = self._c
        return f"{c.DIM}•{c.RESET} "

    def _fence_marker(self, lang: str) -> str:
        c = self._c
        if self._fence:
            self._fence = False
            self._restyle = False
            return f"{c.DIM}{'─' * _FENCE_WIDTH}{c.RESET}"
        self._fence = True
        label = f" {lang.strip()} " if lang.strip() else ""
        return f"{c.DIM}{'─' * 4}{label}{'─' * max(2, _FENCE_WIDTH - 4 - len(label))}{c.RESET}"

    def _flush_pending(self) -> str:
        """Release held markers at a line break or the end of the text. A
        one- or two-backtick run there is a span boundary (`x` at the end of
        a line is the common case); everything else is literal."""
        p, self._pending = self._pending, ""
        if p and set(p) == {"`"} and len(p) < 3 and not self._fence:
            return self._toggle_code()
        return p

    # -- the state machine ----------------------------------------------------

    def _char(self, ch: str) -> str:
        if self._fence_line is not None:
            if ch == "\n":
                label = self._fence_line.strip()
                self._fence_line = None
                self._bol = True
                if not self._fence and " " in label:
                    return "```" + label + "\n"   # "```this is prose": not a fence
                return self._fence_marker(label) + "\n"
            # A closing fence takes anything after the backticks (trailing
            # spaces, a stray word); an opening one only a language name.
            ok = self._fence or (ch in _FENCE_LABEL_CHARS and len(self._fence_line) < _FENCE_LABEL_MAX)
            if ok or ch in " \r":
                self._fence_line += ch
                return ""
            # Not a fence after all ("```" followed by prose, or by code the
            # model failed to break onto its own line): release the backticks
            # and the label literally and carry on with the line.
            held, self._fence_line = "```" + self._fence_line, None
            self._bol = False
            return held + self._inline_char(ch)
        if ch == "\n":
            # Bold and code spans may continue on the next line (a soft
            # break); a blank line ends them. Headings end with their line.
            out = self._flush_pending()
            carry = (self._bold or self._code) and not self._bol
            already_reset = self._restyle   # a carried span was reset at the previous break
            open_style = (self._bold or self._code or self._heading) and not already_reset
            out += (self._c.RESET if open_style else "") + "\n"
            self._heading = False
            if carry:
                self._restyle = True
            else:
                self._bold = self._code = False
                self._restyle = False
            self._bol = True
            return out
        if self._restyle and ch != "\n":
            self._restyle = False
            return self._style() + (self._bol_char(ch) if self._bol else self._inline_char(ch))
        if self._bol:
            return self._bol_char(ch)
        return self._inline_char(ch)

    def _bol_char(self, ch: str) -> str:
        p = self._pending
        # A fence opener or closer: three backticks at the start of a line.
        if ch == "`" and p in ("", "`", "``"):
            p += "`"
            if p == "```":
                self._pending = ""
                self._fence_line = ""
                return ""
            self._pending = p
            return ""
        if p and set(p) == {"`"}:
            # One or two backticks then something else: inline code after all.
            self._pending = ""
            self._bol = False
            return "".join(self._inline_char(b) for b in p) + self._inline_char(ch)
        if self._fence:
            self._bol = False
            return ch
        if ch == " " and p == "":
            return " "   # indentation: still at the start for bullet purposes
        if ch == "#" and (p == "" or set(p) == {"#"}) and len(p) < 6:
            self._pending = p + "#"
            return ""
        if p and set(p) == {"#"}:
            self._pending = ""
            self._bol = False
            if ch == " ":
                self._heading = True
                return self._c.BOLD
            return p + self._inline_char(ch)
        if p == "" and ch in "-*+":
            self._pending = ch
            return ""
        if p in ("-", "+", "*"):
            self._pending = ""
            self._bol = False
            if ch == " ":
                return self._bullet()
            if p == "*" and ch == "*":
                self._pending = "**"   # decided by what follows (see _inline_char)
                return ""
            return p + self._inline_char(ch)
        self._bol = False
        return self._inline_char(ch)

    def _inline_char(self, ch: str) -> str:
        if self._fence:
            return ch
        p = self._pending
        if p == "**":
            # An opening "**" must be followed by something other than a
            # space (CommonMark's left-flanking rule); "next** x" is literal.
            self._pending = ""
            if ch in " \t":
                return "**" + self._inline_char(ch)
            return self._toggle_bold() + self._inline_char(ch)
        if p == "*":
            self._pending = ""
            if ch == "*":
                if self._bold or self._code:
                    return self._toggle_bold()   # closing (or literal inside code)
                self._pending = "**"
                return ""
            return "*" + self._inline_char(ch)
        if p and set(p) == {"`"}:
            # A run of backticks resolves on the next character: one or two
            # open/close a code span, three mid-line are literal (a fence
            # that never got its own line).
            if ch == "`" and len(p) < 3:
                self._pending = p + "`"
                return ""
            self._pending = ""
            head = "```" if len(p) == 3 else self._toggle_code()
            return head + self._inline_char(ch)
        if ch == "*" and not self._code:
            self._pending = "*"
            return ""
        if ch == "`":
            self._pending = "`"
            return ""
        return ch


def render_markdown(text: str) -> str:
    """The whole-text form, for answers that did not stream."""
    md = MarkdownStream()
    return md.feed(text) + md.finish()
