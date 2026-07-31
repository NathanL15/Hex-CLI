#!/usr/bin/env python3
"""hexcli.diffview — render what the agent actually changed.

Until now a file mutation showed only "◆ edit_file" and the user had to trust
it. The harness already holds both sides (undo snapshots are captured before
the first write to each path), so rendering a diff costs ZERO model tokens and
no latency — the cheapest trust feature available on 15 tok/s hardware.

Pure formatting: takes before/after text, returns coloured lines. No I/O.
"""
from __future__ import annotations

import difflib

from .ui import C

_MAX_HUNK_LINES = 40      # per file, before eliding the middle
_MAX_LINE_CHARS = 200


def render_diff(before: str | None, after: str, path: str,
                color: bool = True, max_lines: int = _MAX_HUNK_LINES) -> str:
    """Unified diff for one file. `before=None` means the file was created."""
    if before is None:
        lines = after.splitlines()
        shown = lines[:max_lines]
        out = [_head(f"+ created {path} ({len(lines)} lines)", color)]
        out += [_add("+" + _clip(ln), color) for ln in shown]
        if len(lines) > max_lines:
            out.append(_dim(f"  … {len(lines) - max_lines} more lines", color))
        return "\n".join(out)

    if before == after:
        return _dim(f"  {path}: no change", color)

    diff = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        lineterm="", n=2,
    ))
    # Drop the ---/+++ header lines; the path is in our own header.
    body = [ln for ln in diff[2:] if ln]
    added = sum(1 for ln in body if ln.startswith("+"))
    removed = sum(1 for ln in body if ln.startswith("-"))

    out = [_head(f"~ {path}  (+{added} −{removed})", color)]
    if len(body) > max_lines:
        head_n = max_lines // 2
        tail_n = max_lines - head_n
        shown = body[:head_n] + [f"… {len(body) - max_lines} more diff lines …"] + body[-tail_n:]
    else:
        shown = body
    for ln in shown:
        clipped = _clip(ln)
        if ln.startswith("+"):
            out.append(_add(clipped, color))
        elif ln.startswith("-"):
            out.append(_rem(clipped, color))
        elif ln.startswith("@@"):
            out.append(_dim(clipped, color))
        else:
            out.append(_plain(clipped, color))
    return "\n".join(out)


def render_turn_diffs(snapshots: dict[str, str | None],
                      read_current, color: bool = True) -> str:
    """Diffs for every path a turn touched.

    `snapshots` is the undo map {resolved_path: original_or_None};
    `read_current(path)` returns the file's text now (or None if deleted).
    """
    blocks: list[str] = []
    for path, before in snapshots.items():
        try:
            after = read_current(path)
        except Exception:
            continue
        if after is None:
            blocks.append(_head(f"- deleted {path}", color))
            continue
        blocks.append(render_diff(before, after, path, color=color))
    return "\n".join(blocks)


def _clip(line: str) -> str:
    return line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + " …"


def _head(t: str, color: bool) -> str:
    return f"{C.BCYAN}{t}{C.RESET}" if color else t


def _add(t: str, color: bool) -> str:
    return f"{C.GREEN}{t}{C.RESET}" if color else t


def _rem(t: str, color: bool) -> str:
    return f"{C.RED}{t}{C.RESET}" if color else t


def _dim(t: str, color: bool) -> str:
    return f"{C.DIM}{t}{C.RESET}" if color else t


def _plain(t: str, color: bool) -> str:
    return t
