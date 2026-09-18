#!/usr/bin/env python3
"""evals/test_editing.py — Unit tests for the SEARCH/REPLACE applier
(hexcli.editing): the match tiers, the merge tier, the escape decoding.
Fast, offline, no LLM.

Usage:
    python evals/test_editing.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli import editing as p2  # noqa: E402

# ---------------------------------------------------------------------------
# apply_search_replace
# ---------------------------------------------------------------------------

CONTENT = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "def sub(a, b):\n"
    "    return a - b\n"
)


def test_apply_exact_unique() -> None:
    out, err = p2.apply_search_replace(CONTENT, [("    return a + b", "    return a + b + 0")])
    assert not err
    assert "a + b + 0" in out


def test_apply_ambiguous_is_error_not_guess() -> None:
    content = "x = 1\nx = 1\n"
    out, err = p2.apply_search_replace(content, [("x = 1", "x = 2")])
    assert out is None
    assert "2 locations" in err


def test_apply_trailing_whitespace_tier() -> None:
    content = "line one   \nline two\n"
    out, err = p2.apply_search_replace(content, [("line one", "line ONE")])
    assert not err, err
    assert "line ONE" in out


def test_apply_indent_shift_tier() -> None:
    # Model forgot the indentation — the applier re-indents the replacement.
    out, err = p2.apply_search_replace(CONTENT, [("return a - b", "return a - b  # fixed")])
    assert not err, err
    assert "    return a - b  # fixed" in out


def test_apply_no_match_reports_closest_region() -> None:
    out, err = p2.apply_search_replace(CONTENT, [("    return a * b", "    return a / b")])
    assert out is None
    assert "closest region" in err and "lines" in err
    assert "EXACTLY" in err


PROCESSOR = (
    "def process_data(items: list) -> list:\n"
    "    result = []\n"
    "    for item in items:\n"
    "        if item > 0:\n"
    "            result.appned(item * 2)   # Bug: appned should be append\n"
    "    return result\n"
)
PROCESSOR_FIXED = PROCESSOR.replace("result.appned(", "result.append(")


def test_delta_transfer_rebuilt_lines_one_token_swap() -> None:
    # uc1-t3 shape: the model rebuilt the lines from the traceback ("data"
    # for "items", comment dropped) but the change it wants is appned->append.
    # The substitution is transferred; the hallucinated names never land.
    out, err = p2.apply_search_replace(PROCESSOR, [(
        "    for item in data:\n        if item > 0:\n            result.appned(item * 2)\n    return result",
        "    for item in data:\n        if item > 0:\n            result.append(item * 2)\n    return result",
    )])
    assert not err, err
    assert out == PROCESSOR_FIXED
    assert p2.LAST_APPLY_TIER == "transfer"
    p2.apply_search_replace(PROCESSOR, [("    return result", "    return result")])
    assert p2.LAST_APPLY_TIER == "exact"


def test_delta_transfer_disambiguates_against_same_line_comment() -> None:
    # "appned" occurs twice on the line (code and comment); "appned(" is unique.
    out, err = p2.apply_search_replace(PROCESSOR, [("    appned(item)", "    append(item)")])
    assert not err, err
    assert out == PROCESSOR_FIXED


def test_delta_transfer_refuses_whitespace_only_context() -> None:
    # "appned " (with a space) would match only the comment; a space is not
    # an anchor, so this must stay an error rather than edit the comment.
    out, err = p2.apply_search_replace(PROCESSOR, [("    appned to new_list:", "    append to new_list:")])
    assert out is None and "closest region" in err
    out, err = p2.apply_search_replace(PROCESSOR, [(
        "    for num in numbers:\n        if num > 0:\n            appned numbers.append(num * 2)\n",
        "    for num in numbers:\n        if num > 0:\n            numbers.append(num * 2)\n",
    )])
    assert out is None and "closest region" in err


def test_delta_transfer_refuses_duplicated_neighbour() -> None:
    # Would produce "result.result.append(" — the replacement restates the
    # prefix already in the file.
    out, err = p2.apply_search_replace(PROCESSOR, [
        ("            appned(item * 2)\n", "            result.append(item * 2)\n"),
    ])
    assert out is None and "closest region" in err


def test_delta_transfer_refuses_insert_noop_and_ambiguous() -> None:
    # An insertion whose only anchor is a token the file does not have
    # there is refused (nothing here matches "for x in data" closely enough).
    out, err = p2.apply_search_replace(PROCESSOR, [
        ("    for x in data:\n        print(x)", "    for x in data:\n        print(x)\n        print(1)"),
    ])
    assert out is None
    # old == new: nothing to transfer.
    out, err = p2.apply_search_replace(PROCESSOR, [("    data.append(item)", "    data.append(item)")])
    assert out is None
    # Fragment ambiguous everywhere and no context resolves it.
    content = "a = foo(1)\nb = foo(2)\n"
    out, err = p2.apply_search_replace(content, [("c = foo(3)", "c = bar(3)")])
    assert out is None


def test_delta_transfer_insert_beside_misremembered_token() -> None:
    # uc1-t4 shape: file says avg, the model wrote average and adds a key.
    # Before: the 95% paste installed "average" (NameError in 5 of 6 live
    # runs). Now the insertion lands beside the file's own "avg".
    content = 'def stats(d):\n    return {"total": total, "count": count, "average": avg}\n'
    out, err = p2.apply_search_replace(content, [(
        '    return {"total": total, "count": count, "average": average}',
        '    return {"total": total, "count": count, "average": average, "median": median}',
    )])
    assert not err, err
    assert out == 'def stats(d):\n    return {"total": total, "count": count, "average": avg, "median": median}\n'
    # But a change ON the misremembered token is a conflict, never a paste.
    out, err = p2.apply_search_replace(content, [(
        '    return {"total": total, "count": count, "average": average}',
        '    return {"total": total, "count": count, "average": mean}',
    )])
    assert out is None


def test_delta_transfer_insert_new_line_before_anchor() -> None:
    content = "def f(x):\n    return x\n"
    out, err = p2.apply_search_replace(content, [("    return y", "    y = x + 1\n    return y")])
    assert not err, err
    assert out == "def f(x):\n    y = x + 1\n    return y\n" or out == "def f(x):\n    y = x + 1\n    return x\n"
    # (the model misremembered "x" as "y"; the file keeps its own name)
    assert out == "def f(x):\n    y = x + 1\n    return x\n"


def test_unescaped_json_old_string() -> None:
    # agentic-3 (2026-09-13): the model escaped its JSON arguments twice.
    content = '{\n  "name": "demo"\n}\n'
    out, err = p2.apply_search_replace(content, [
        ('\\"name\\": \\"demo\\"', '\\"name\\": \\"demo\\",\\n  \\"version\\": \\"1.0\\"'),
    ])
    assert not err, err
    assert out == '{\n  "name": "demo",\n  "version": "1.0"\n}\n'
    assert p2.LAST_APPLY_TIER == "unescaped"
    # A file that really contains the escaped form matches it exactly first.
    content2 = 'x = "say \\"hi\\""\n'
    out, err = p2.apply_search_replace(content2, [('\\"hi\\"', '\\"yo\\"')])
    assert not err and out == 'x = "say \\"yo\\""\n'


def test_delta_transfer_inserted_line_keeps_line_structure() -> None:
    # Whichever side the token diff attaches the line break to, the result
    # must be one new line between the two existing ones.
    content = "x = 1\ny = 2\nz = 3\n"
    for old, new in [
        ("x = 1\ny = 22", "x = 1\nw = 0\ny = 22"),
        ("y = 22\nz = 3", "y = 22\nw = 0\nz = 3"),
        ("x = 1\ny = 22\nz = 3", "x = 1\ny = 22\nw = 0\nz = 3"),
    ]:
        out, err = p2.apply_search_replace(content, [(old, new)])
        assert not err, (old, err)
        assert out.count("\n") == 4 and "w = 0" in out.splitlines(), (old, out)
        assert "y = 2" in out and "y = 22" not in out, (old, out)


def test_looks_double_escaped() -> None:
    assert p2.looks_double_escaped("import re\\n\\n\\n# Regex")
    assert not p2.looks_double_escaped("import re\n\n# Regex")          # real newlines
    assert not p2.looks_double_escaped('pattern = "a\\nb"')              # one literal, one line
    assert not p2.looks_double_escaped("a\\nb\n c\\nd")                 # mixed: has a real newline
    assert p2.unescape_json("import re\\n\\n\\n# Regex") == "import re\n\n\n# Regex"
    # Quotes, the runit-1 shape: every quote escaped, no bare one.
    assert p2.looks_double_escaped('print(\\"Hello, world\\")')
    assert p2.looks_double_escaped('a = \\"x\\"\nb = \\"y\\"\n')            # several lines, still no bare quote
    assert not p2.looks_double_escaped('print("Hello, world")')             # ordinary
    assert not p2.looks_double_escaped('s = "say \\"hi\\""\n')             # escaped quotes inside a real string
    assert not p2.looks_double_escaped("x = 'a'\n")                        # no double quote at all
    assert p2.unescape_json('print(\\"Hello, world\\")') == 'print("Hello, world")'


def test_delta_transfer_two_hunks_within_region() -> None:
    # old_string is ~95% similar to the file (x written as y); before this
    # tier the closest-match paste installed the model's "y". The transfer
    # keeps the file's "x" and applies only totl->total.
    content = "def f(x):\n    totl = x + 1\n    return totl\n"
    out, err = p2.apply_search_replace(content, [
        ("def f(y):\n    totl = y + 1\n    return totl", "def f(y):\n    total = y + 1\n    return total"),
    ])
    assert not err, err
    assert out == "def f(x):\n    total = x + 1\n    return total\n"


def test_apply_blocks_in_order() -> None:
    out, err = p2.apply_search_replace(CONTENT, [
        ("    return a + b", "    return a + b  # one"),
        ("    return a - b", "    return a - b  # two"),
    ])
    assert not err
    assert "# one" in out and "# two" in out


TESTS = [
    test_apply_exact_unique,
    test_apply_ambiguous_is_error_not_guess,
    test_apply_trailing_whitespace_tier,
    test_apply_indent_shift_tier,
    test_apply_no_match_reports_closest_region,
    test_delta_transfer_rebuilt_lines_one_token_swap,
    test_delta_transfer_disambiguates_against_same_line_comment,
    test_delta_transfer_refuses_whitespace_only_context,
    test_delta_transfer_refuses_duplicated_neighbour,
    test_delta_transfer_refuses_insert_noop_and_ambiguous,
    test_delta_transfer_insert_beside_misremembered_token,
    test_delta_transfer_insert_new_line_before_anchor,
    test_unescaped_json_old_string,
    test_delta_transfer_inserted_line_keeps_line_structure,
    test_looks_double_escaped,
    test_delta_transfer_two_hunks_within_region,
    test_apply_blocks_in_order,
]


def _run(fn: Any) -> bool:
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as exc:
        print(f"  FAIL  {fn.__name__}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {fn.__name__}: {exc!r}")
        return False


def main() -> int:
    print(f"\nevals/test_editing.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
