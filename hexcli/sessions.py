#!/usr/bin/env python3
"""hexcli.sessions — session objects and the on-disk history store.

Lifted out of agent.py unchanged. Owns HISTORY_PATH, because the only readers
of it are the two functions here; keeping the path next to them is what makes
the store redirectable in tests.

`evals/test_core.py` patches `sessions.HISTORY_PATH` to a temp file. That patch
MUST target this module: these functions resolve the name in their own
namespace, so patching a re-exported copy on hexcli.agent would silently do
nothing and the suite would write to the real history.json while still passing.

Checkpoints (/save, /load) deliberately stay in agent.py — they capture a
workspace_snapshot, which belongs with the tools.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from hexcli.ui import C, cprint

# Project root, derived the same way agent.APP_DIR is. Asserted equal in
# evals/test_core.py so the two definitions cannot drift apart.
APP_DIR = Path(__file__).resolve().parent.parent
HISTORY_PATH = APP_DIR / "history.json"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def create_session() -> dict[str, Any]:
    now = iso_now()
    return {
        "id": str(uuid4()),
        "title": "New Chat",
        "created_at": now,
        "modified_at": now,
        "messages": [],
        "compact_count": 0,
    }


def session_has_messages(session: dict[str, Any]) -> bool:
    msgs = session.get("messages")
    return isinstance(msgs, list) and len(msgs) > 0


def generate_session_title(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\s-]", "", text).strip()
    words = [w for w in cleaned.split() if w]
    if not words:
        return "New Chat"
    return " ".join(w.upper() if w.isupper() else w.capitalize() for w in words[:6])


def touch_session(session: dict[str, Any]) -> None:
    session["modified_at"] = iso_now()


def append_session_message(session: dict[str, Any], role: str, content: str) -> None:
    msgs = session.setdefault("messages", [])
    if not isinstance(msgs, list):
        session["messages"] = []
        msgs = session["messages"]
    if not session_has_messages(session) and role == "user":
        session["title"] = generate_session_title(content)
    msgs.append({"role": role, "content": content})
    touch_session(session)


def sort_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _epoch = datetime.min.replace(tzinfo=UTC)

    def _key(s: dict[str, Any]) -> datetime:
        raw = s.get("modified_at", "")
        try:
            return parse_timestamp(str(raw))
        except (ValueError, TypeError):
            return _epoch

    return sorted(sessions, key=_key, reverse=True)


def save_history_store(sessions: list[dict[str, Any]]) -> None:
    payload = json.dumps({"sessions": sort_sessions(sessions)}, indent=2) + "\n"
    tmp = HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def load_history_store(config: dict[str, Any]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if HISTORY_PATH.exists():
        # A truncated or corrupted history file used to raise here and take
        # the whole app down on EVERY launch — unrecoverable without knowing
        # to delete a file you were never told about. Past history is never
        # worth more than a working CLI: quarantine it and carry on.
        try:
            with HISTORY_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            raw = data.get("sessions", []) if isinstance(data, dict) else []
            sessions = [s for s in raw if isinstance(s, dict)]
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            quarantine = HISTORY_PATH.with_suffix(".corrupt")
            try:
                HISTORY_PATH.replace(quarantine)
                cprint(
                    f"\n  History file was unreadable ({exc.__class__.__name__}). "
                    f"Moved it to {quarantine.name} and started a fresh history.",
                    C.YELLOW,
                )
            except OSError:
                cprint("\n  History file is unreadable and could not be moved; "
                       "continuing with an empty history.", C.YELLOW)
            sessions = []

    cutoff = utc_now() - timedelta(days=int(config.get("history_retention_days", 30)))
    filtered: list[dict[str, Any]] = []
    changed = False
    for s in sessions:
        try:
            modified_at = parse_timestamp(str(s.get("modified_at", "")))
        except ValueError:
            changed = True
            continue
        if modified_at < cutoff:
            changed = True
            continue
        s.setdefault("title", "New Chat")
        s.setdefault("created_at", s.get("modified_at", iso_now()))
        s.setdefault("messages", [])
        s.setdefault("compact_count", 0)
        filtered.append(s)

    filtered = sort_sessions(filtered)
    if changed:
        save_history_store(filtered)
    return filtered


def search_sessions(
    sessions: list[dict[str, Any]],
    term: str,
    max_snippets: int = 2,
    context: int = 44,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over titles and message content.

    Returns hits in the same order as `sessions`, each carrying the 1-based
    index into that list — the SAME number /history shows and /resume takes,
    so a search result is directly resumable. Snippets come pre-split as
    (role, prefix, match, suffix) so the renderer can highlight the match
    without re-finding it.
    """
    term_l = term.lower()
    if not term_l:
        return []
    hits: list[dict[str, Any]] = []
    for idx, session in enumerate(sessions, start=1):
        raw_matches: list[tuple[str, str, int]] = []
        title = str(session.get("title", ""))
        pos = title.lower().find(term_l)
        if pos >= 0:
            raw_matches.append(("title", title, pos))
        for msg in session.get("messages", []):
            if len(raw_matches) >= max_snippets:
                break
            if not isinstance(msg, dict):
                continue
            content = str(msg.get("content", ""))
            pos = content.lower().find(term_l)
            if pos >= 0:
                raw_matches.append((str(msg.get("role", "?")), content, pos))
        if not raw_matches:
            continue
        snippets: list[tuple[str, str, str, str]] = []
        for role, text, pos in raw_matches[:max_snippets]:
            start = max(0, pos - context)
            end = min(len(text), pos + len(term) + context)
            prefix = ("…" if start > 0 else "") + text[start:pos].replace("\n", " ")
            match = text[pos:pos + len(term)].replace("\n", " ")
            suffix = text[pos + len(term):end].replace("\n", " ") + ("…" if end < len(text) else "")
            snippets.append((role, prefix, match, suffix))
        hits.append({"index": idx, "session": session, "snippets": snippets})
    return hits


def upsert_session(sessions: list[dict[str, Any]], session: dict[str, Any]) -> None:
    if not session_has_messages(session):
        return
    for i, existing in enumerate(sessions):
        if existing.get("id") == session.get("id"):
            sessions[i] = session
            return
    sessions.append(session)


def sync_session_store(sessions: list[dict[str, Any]], session: dict[str, Any]) -> None:
    upsert_session(sessions, session)
    save_history_store(sessions)


