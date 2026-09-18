#!/usr/bin/env python3
"""hexcli.editing — the SEARCH/REPLACE applier behind edit_file.

Match tiers, in order: exact; trailing-whitespace-insensitive; uniform
indent shift; a token-level three-way merge against the file's closest
region (the model's change lands where the file agrees with what it saw,
misremembered tokens keep the file's text, conflicts refuse). Ambiguity is
an error, never a guess; a miss reports the closest region. Double-escaped
old_strings are decoded once and retried. Extracted from the retired
protocol_v2 module on 2026-09-14; the tiers themselves date from the v1.7
audit (2026-07) through the merge tier (2026-09-13).
"""
from __future__ import annotations

import difflib
import ast
import re

# Tier that landed the most recent successful block: "exact", "whitespace",
# "indent", "transfer" or "closest". Read by tools.edit_file_tool for its
# event line so arm logs show which edits the fuzzy tiers rescued.
LAST_APPLY_TIER = "exact"


def apply_search_replace(content: str, blocks: list[tuple[str, str]]) -> tuple[str | None, str]:
    """Apply blocks in order. Returns (new_content, "") or (None, error).

    Match tiers per block:
      1. exact unique substring
      2. trailing-whitespace-insensitive unique line-window match
      3. leading-indent-shifted unique match (replacement is re-indented by
         the observed delta)
    Ambiguity (multiple matches at any tier) is an error, not a guess.
    """
    for n, (search, replace) in enumerate(blocks, 1):
        new_content, err = _apply_one(content, search, replace, n)
        if err:
            return None, err
        content = new_content
    return content, ""


def _apply_one(content: str, search: str, replace: str, block_no: int) -> tuple[str, str]:
    # Tier 1 — exact.
    global LAST_APPLY_TIER
    count = content.count(search)
    if count == 1:
        LAST_APPLY_TIER = "exact"
        return content.replace(search, replace, 1), ""
    if count == 0 and _ESCAPED_RE.search(search):
        # The model escaped its JSON arguments twice (agentic-3, 2026-09-13:
        # old_string '\"name\": \"demo\"' for a file holding "name": "demo").
        # Decode the JSON escapes once, in both strings, and retry from the top
        # when that form actually exists in the file.
        decoded = _unescape_json(search)
        if decoded != search and decoded in content:
            out, err = _apply_one(content, decoded, _unescape_json(replace), block_no)
            if not err and LAST_APPLY_TIER == "exact":
                LAST_APPLY_TIER = "unescaped"
            return out, err
    if count > 1:
        return "", (f"SEARCH block {block_no} matches {count} locations in the file — "
                    "add surrounding lines to make it unique.")

    c_lines = content.split("\n")
    s_lines = search.split("\n")
    win = len(s_lines)

    def _window_matches(norm) -> list[int]:
        target = [norm(line) for line in s_lines]
        hits = []
        for i in range(len(c_lines) - win + 1):
            if [norm(line) for line in c_lines[i:i + win]] == target:
                hits.append(i)
        return hits

    # Tier 2 — trailing whitespace insensitive.
    hits = _window_matches(lambda ln: ln.rstrip())
    if len(hits) == 1:
        i = hits[0]
        new_lines = c_lines[:i] + replace.split("\n") + c_lines[i + win:]
        LAST_APPLY_TIER = "whitespace"
        return "\n".join(new_lines), ""
    if len(hits) > 1:
        return "", (f"SEARCH block {block_no} matches {len(hits)} locations "
                    "(ignoring trailing whitespace) — add more context lines.")

    # Tier 3 — uniform leading-indent shift.
    hits = _window_matches(lambda ln: ln.strip())
    if len(hits) == 1:
        i = hits[0]
        # Compute indent delta from the first non-blank pair.
        delta = ""
        sign = 1
        for file_ln, search_ln in zip(c_lines[i:i + win], s_lines):
            if file_ln.strip():
                file_ind = file_ln[:len(file_ln) - len(file_ln.lstrip())]
                search_ind = search_ln[:len(search_ln) - len(search_ln.lstrip())]
                if len(file_ind) >= len(search_ind):
                    delta, sign = file_ind[len(search_ind):], 1
                else:
                    delta, sign = search_ind[len(file_ind):], -1
                break
        adjusted: list[str] = []
        for ln in replace.split("\n"):
            if not ln.strip():
                adjusted.append(ln)
            elif sign > 0:
                adjusted.append(delta + ln)
            else:
                adjusted.append(ln[len(delta):] if ln.startswith(delta) else ln)
        new_lines = c_lines[:i] + adjusted + c_lines[i + win:]
        LAST_APPLY_TIER = "indent"
        return "\n".join(new_lines), ""
    if len(hits) > 1:
        return "", (f"SEARCH block {block_no} matches {len(hits)} locations "
                    "(ignoring indentation) — add more context lines.")

    # Tier 4 — three-way merge against the closest region. Multi-turn traces
    # (uc1-t3/t4, 2026-09-13) show the 4B rebuilding the lines from memory
    # instead of copying them: "for item in data" where the file says "items",
    # "average" where the file says "avg", a dropped trailing comment. Its
    # old_string then lands at 40-95% similarity while the CHANGE it wants
    # (old_string -> new_string) is small and clear. Pasting new_string over
    # the region installs the misremembered names (five of six uc1-t4 runs
    # ended in NameError: 'average' that way), so the change is merged
    # instead: token hunks the model changed are applied where the file
    # agrees with what the model saw, tokens the model misremembered keep the
    # file's text, and a hunk that touches a misremembered token refuses.
    merged = _delta_transfer(content, search, replace)
    if merged is not None:
        LAST_APPLY_TIER = "transfer"
        return merged, ""

    # No match at any tier — report the closest region.
    return "", _no_match_error(c_lines, s_lines, block_no)


def unescape_json(text: str) -> str:
    """Decode one level of JSON escapes (\\" \\n \\t \\\\)."""
    return _unescape_json(text)


_QUOTE_ADJACENT_NL = re.compile(r'(["\'])\\n|\\n(["\'])')


def unescape_body(text: str, path: str) -> str:
    """Decode a double-escaped file body, keeping a Python source parseable.

    A body the model escaped twice carries its line breaks as literal \\n --
    and so does every \\n it meant as a string escape inside the code:
    `print(\\"\\nWelcome\\")` and the line break after it are the same two
    characters. Decoding all of them (the 2.9.0 rule) turns the escape into a
    real newline inside the string literal, and the file the owner got on
    2026-09-18 read `print("` / `Welcome to the Calculator App!")` -- a
    SyntaxError the model then spent eleven steps not fixing.

    For a .py path, decode everything and parse; if that fails, decode
    everything EXCEPT a \\n that sits right after an opening quote or right
    before a closing one, and parse again; keep whichever parses, and the
    full decode when neither does (the caller's checker will say so). Other
    file types get the full decode as before. Measured over the 511
    write_file bodies on record: five are double-escaped, two of those have
    a quote-adjacent \\n, and one of the two (the session above) parses only
    this way."""
    full = _unescape_json(text)
    if not path.lower().endswith(".py"):
        return full
    try:
        ast.parse(full)
        return full
    except SyntaxError:
        pass
    kept = _QUOTE_ADJACENT_NL.sub(lambda m: (m.group(1) or "") + "\x00" + (m.group(2) or ""), text)
    candidate = _unescape_json(kept).replace("\x00", "\\n")
    try:
        ast.parse(candidate)
        return candidate
    except SyntaxError:
        return full


_DELTA_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")
_ESCAPED_RE = re.compile(r'\\["nt\\]')
_BARE_QUOTE_RE = re.compile(r'(?<!\\)"')
_DELTA_MAX_HUNKS = 4
_DELTA_MAX_REPLACED_CHARS = 40
_DELTA_MAX_INSERTED_CHARS = 400
_DELTA_MIN_MATCHED = 0.6   # fraction of old_string's tokens found in the window


def looks_double_escaped(text: str) -> bool:
    """A file body JSON-escaped a second time, in either of the two shapes
    the model actually produces.

    Newlines: two or more literal backslash-n sequences and not a single
    real line break (write_file, persona_guard 2026-09-12: 'import
    re\\n\\n\\n# Regex …' written as one line). A real one-line file that
    spells "\\n" on purpose is rarer than the model's habit; anything with a
    genuine newline is left alone.

    Quotes: at least one backslash-quote and not a single bare quote
    (runit-1, 2026-09-17: `print(\\"Hello, world\\")` as the whole of
    hello.py, a SyntaxError the model then "fixed" by editing the line to
    itself until the step limit). Over the 511 write_file bodies on record
    11 have this shape and every one is that defect; none has both escaped
    and bare quotes, so a body with even one bare quote is left alone."""
    if "\n" not in text and text.count("\\n") >= 2:
        return True
    return '\\"' in text and _BARE_QUOTE_RE.search(text) is None


def _unescape_json(text: str) -> str:
    return _ESCAPED_RE.sub(lambda m: {'"': '"', "n": "\n", "t": "\t", "\\": "\\"}[m.group(0)[1]], text)


def _keys(toks: list[str]) -> list[str]:
    """Token keys for matching: whitespace runs compare equal to each other,
    but a run holding a newline never equals one that does not (a line break
    the model wrote must not be absorbed by the file's indentation)."""
    return [("\n" if "\n" in t else " ") if t.isspace() else t for t in toks]


def _delta_transfer(content: str, search: str, replace: str) -> str | None:
    """Merge the search->replace change into content's closest region, or None.

    Three sequences: old (what the model believes the region says), new (what
    it wants), window (what the file says). Hunks of old->new are applied to
    the window where the hunk's tokens are equal between old and window; the
    rest of the window is kept verbatim, so nothing the model misremembered
    reaches the file. Refusals, each a documented failure shape: more than
    four hunks; a replaced fragment over 40 chars or an insertion over 400;
    no window at 60% token similarity or a tie between windows; a replaced
    token the file does not have at that spot (unless the "change" merely
    restates the file, which is skipped); a hunk with no stable neighbour on
    either side; a hunk that grows on a side the model did not see correctly;
    an insertion whose anchor sits next to tokens the model never saw; a
    replacement that restates the tokens already beside it.
    """
    while search.endswith("\n") and replace.endswith("\n"):
        search, replace = search[:-1], replace[:-1]
    s_tok = _DELTA_TOKEN_RE.findall(search)
    r_tok = _DELTA_TOKEN_RE.findall(replace)
    if not s_tok or "".join(s_tok) != search or "".join(r_tok) != replace:
        return None
    s_keys = _keys(s_tok)
    b_ops = [op for op in difflib.SequenceMatcher(None, s_keys, _keys(r_tok), autojunk=False).get_opcodes()
             if op[0] != "equal"]
    if not b_ops or len(b_ops) > _DELTA_MAX_HUNKS:
        return None
    for _, i1, i2, j1, j2 in b_ops:
        if (len("".join(s_tok[i1:i2])) > _DELTA_MAX_REPLACED_CHARS
                or len("".join(r_tok[j1:j2])) > _DELTA_MAX_INSERTED_CHARS):
            return None

    window = _delta_window(content, s_keys, search.count("\n") + 1)
    if window is None:
        return None
    start, end, w_tok = window
    eq: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, s_keys, _keys(w_tok), autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                eq[i1 + k] = j1 + k

    edits: list[tuple[int, int, str]] = []
    for tag, i1, i2, j1, j2 in b_ops:
        after = "".join(r_tok[j1:j2])
        if tag == "insert":
            span = _delta_insert_span(s_tok, w_tok, eq, i1)
        else:
            i1, i2, after = _delta_trim(s_tok, i1, i2, after)
            if i1 >= i2:
                return None
            span = _delta_replace_span(s_tok, w_tok, eq, i1, i2, after)
        if span is None:
            return None
        if span == ():
            continue  # the "change" restates the file; nothing to do
        ws, we = span
        if _delta_duplicates_neighbour(w_tok, ws, we, after):
            return None
        if _ESCAPED_RE.search(after) and "\\" not in "".join(w_tok):
            # The model escaped part of its arguments a second time; the file
            # holds no backslashes, so those would land literally (a uc1-t4
            # attempt wrote '\\"median\\": median' into Python).
            return None
        if "\n" in after:
            after = _delta_reindent(after, s_tok, i1, w_tok, ws)
            if ws == we and ws > 0 and "\n" in w_tok[ws - 1]:
                lead = _DELTA_TOKEN_RE.match(after)
                if lead and lead.group(0).isspace() and "\n" in lead.group(0) and not after.endswith(("\n", w_tok[ws - 1])):
                    # Inserted before a line-leading anchor with the file's
                    # own line break already in place: the insert's leading
                    # break belongs after its text, not before it.
                    after = after[lead.end():] + w_tok[ws - 1]
        edits.append((ws, we, after))
    if not edits:
        return None
    edits.sort()
    for (_, a_end, _), (b_start, _, _) in zip(edits, edits[1:]):
        if a_end > b_start:
            return None
    out = list(w_tok)
    for ws, we, after in reversed(edits):
        out[ws:we] = [after]
    return content[:start] + "".join(out) + content[end:]


def _delta_window(content: str, s_keys: list[str], height: int) -> tuple[int, int, list[str]] | None:
    """The unique best line window (height-1..height+1) by token similarity.

    Ranked by the symmetric ratio (so the tightest window wins over a taller
    one holding the same matches); a tie on that ratio refuses. The floor is
    the fraction of old_string's own tokens found in the window, so a short
    fragment ("    appned(item)") against a long line still qualifies while
    unrelated text does not.
    """
    c_lines = content.split("\n")
    offsets = [0]
    for ln in c_lines:
        offsets.append(offsets[-1] + len(ln) + 1)
    best: tuple[float, float, int, int, list[str]] | None = None
    tie = False
    for h in sorted({max(1, height - 1), height, height + 1}):
        for i in range(0, max(1, len(c_lines) - h + 1)):
            w_tok = _DELTA_TOKEN_RE.findall("\n".join(c_lines[i:i + h]))
            if not w_tok:
                continue
            sm = difflib.SequenceMatcher(None, s_keys, _keys(w_tok), autojunk=False)
            r = sm.ratio()
            if best is None or r > best[0] + 1e-9:
                matched = sum(b.size for b in sm.get_matching_blocks()) / len(s_keys)
                best, tie = (r, matched, i, h, w_tok), False
            elif abs(r - best[0]) <= 1e-9:
                tie = True
    if best is None or tie or best[1] < _DELTA_MIN_MATCHED:
        return None
    r, matched, i, h, w_tok = best
    return offsets[i], offsets[i + h] - 1, w_tok


def _line_indent_at(toks: list[str], i: int) -> str:
    """Leading whitespace of the line holding token i."""
    for k in range(min(i, len(toks) - 1), -1, -1):
        t = toks[k]
        if "\n" in t:
            return t[t.rfind("\n") + 1:]
    return toks[0] if toks and toks[0].isspace() and "\n" not in toks[0] else ""


def _delta_reindent(after: str, s_tok: list[str], i1: int, w_tok: list[str], ws: int) -> str:
    """Shift the lines after the first newline of `after` by the indent delta
    between the model's line and the file's line (the model wrote the whole
    function four spaces deeper in one uc1-t4 attempt; its inserted lines
    must follow the file, not its memory)."""
    old_ind = _line_indent_at(s_tok, i1)
    win_ind = _line_indent_at(w_tok, ws)
    if old_ind == win_ind:
        return after
    first, rest = after.split("\n", 1)
    lines = rest.split("\n")
    out = []
    for n, ln in enumerate(lines):
        last = n == len(lines) - 1
        if ln.startswith(old_ind) and (ln.strip() or last):
            # A blank middle line stays blank; the trailing segment is the
            # indent of the line that follows and shifts like the others.
            out.append(win_ind + ln[len(old_ind):])
        else:
            out.append(ln)
    return first + "\n" + "\n".join(out)


def _delta_trim(s_tok: list[str], i1: int, i2: int, after: str) -> tuple[int, int, str]:
    """Drop whitespace tokens from the edges of a hunk; a space is no anchor."""
    while i1 < i2 and s_tok[i1].isspace():
        i1 += 1
        after = after.lstrip() if after[:1].isspace() else after
    while i2 > i1 and s_tok[i2 - 1].isspace():
        i2 -= 1
        after = after.rstrip() if after[-1:].isspace() else after
    return i1, i2, after


def _nonws_left(toks: list[str], i: int) -> int | None:
    for k in range(i - 1, -1, -1):
        if not toks[k].isspace():
            return k
    return None


def _nonws_right(toks: list[str], i: int) -> int | None:
    for k in range(i, len(toks)):
        if not toks[k].isspace():
            return k
    return None


def _gap_is_ws(w_tok: list[str], a: int, b: int) -> bool:
    """True when only whitespace sits strictly between window indices a and b."""
    return all(w_tok[k].isspace() for k in range(a + 1, b))


def _delta_replace_span(s_tok: list[str], w_tok: list[str], eq: dict[int, int],
                        i1: int, i2: int, after: str) -> tuple[int, int] | tuple[()] | None:
    idx = [k for k in range(i1, i2) if not s_tok[k].isspace()]
    if not idx:
        return None
    lft = _nonws_left(s_tok, i1)
    rgt = _nonws_right(s_tok, i2)
    after_n = sum(1 for t in _DELTA_TOKEN_RE.findall(after) if not t.isspace())
    if any(k not in eq for k in idx):
        # The model is changing a token the file does not have there. Allowed
        # only when its "change" reproduces what the file already says
        # (uc1-t4 fixture: old "cnt", new "count", file "count") — then the
        # hunk is a no-op. Anything else is a conflict.
        if lft is None or rgt is None or lft not in eq or rgt not in eq:
            return None
        if any(not s_tok[k].isspace() for k in range(lft + 1, i1)) or \
                any(not s_tok[k].isspace() for k in range(i2, rgt)):
            return None
        between = [t for t in w_tok[eq[lft] + 1:eq[rgt]] if not t.isspace()]
        wanted = [t for t in _DELTA_TOKEN_RE.findall(after) if not t.isspace()]
        return () if between == wanted else None
    pos = [eq[k] for k in idx]
    for a, b in zip(pos, pos[1:]):
        if b <= a or not _gap_is_ws(w_tok, a, b):
            return None
    left_stable = lft is not None and lft in eq and _gap_is_ws(w_tok, eq[lft], pos[0])
    right_stable = rgt is not None and rgt in eq and _gap_is_ws(w_tok, pos[-1], eq[rgt])
    if not (left_stable or right_stable):
        return None
    if not (left_stable and right_stable) and after_n > len(idx):
        # One side is text the model did not see correctly: a token swap is
        # safe, growing into that side is not ("appned(" -> "result.append("
        # beside a "result." the file already has).
        return None
    return pos[0], pos[-1] + 1


def _delta_insert_span(s_tok: list[str], w_tok: list[str], eq: dict[int, int], b: int) -> tuple[int, int] | None:
    lft = _nonws_left(s_tok, b)
    rgt = _nonws_right(s_tok, b)
    left_ok = lft is not None and lft in eq
    right_ok = rgt is not None and rgt in eq
    if left_ok and right_ok:
        if not _gap_is_ws(w_tok, eq[lft], eq[rgt]):
            return None  # the file has tokens between them the model never saw
        return eq[rgt], eq[rgt]
    if right_ok:
        w = eq[rgt]
        if lft is None and not all(t.isspace() for t in w_tok[:w]):
            return None  # the region starts with tokens the model never saw
        return w, w
    if left_ok:
        w = eq[lft] + 1
        if rgt is None and not all(t.isspace() for t in w_tok[w:]):
            return None
        return w, w
    return None


def _delta_duplicates_neighbour(w_tok: list[str], ws: int, we: int, after: str) -> bool:
    """True when `after` restates what already sits beside the span on its line."""
    a_tok = [t for t in _DELTA_TOKEN_RE.findall(after) if not t.isspace()]
    if len(a_tok) < 2:
        return False
    pre: list[str] = []
    for k in range(ws - 1, -1, -1):
        if "\n" in w_tok[k]:
            break
        if not w_tok[k].isspace():
            pre.append(w_tok[k])
    pre.reverse()
    post: list[str] = []
    for k in range(we, len(w_tok)):
        if "\n" in w_tok[k]:
            break
        if not w_tok[k].isspace():
            post.append(w_tok[k])
    for n in range(1, len(a_tok)):
        if (pre and pre[-n:] == a_tok[:n]) or (post and post[:n] == a_tok[-n:]):
            return True
    return False


def _no_match_error(c_lines: list[str], s_lines: list[str], block_no: int) -> str:
    win = len(s_lines)
    best_ratio, best_i = 0.0, 0
    search_text = "\n".join(s_lines)
    for i in range(max(1, len(c_lines) - win + 1)):
        cand = "\n".join(c_lines[i:i + win])
        ratio = difflib.SequenceMatcher(None, search_text, cand, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio, best_i = ratio, i
    closest = "\n".join(c_lines[best_i:best_i + win])
    return (
        f"SEARCH block {block_no} was not found in the file. "
        f"The closest region is lines {best_i + 1}-{best_i + win} "
        f"(similarity {best_ratio:.0%}):\n---\n{closest}\n---\n"
        "Copy the existing lines EXACTLY (same spelling, spacing, and punctuation) "
        "into the SEARCH section, or use fewer, more distinctive lines."
    )


# ---------------------------------------------------------------------------
# Rendering — actions and tool results as they appear in the conversation
# ---------------------------------------------------------------------------
