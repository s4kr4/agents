#!/usr/bin/env python3
"""One-shot migration from the legacy SQLite memory.db to the file-based stores.

Defaults to a dry run (no writes); pass --apply to write. Safe to re-run: all
writes are idempotent (existing events/observations are skipped by id, and
memory files are always rewritten with the exact same, deterministically
derived content for a given logical key -- see ``_migrate_memories``).

This is the only remaining place in this codebase allowed to depend on
``sqlite3`` -- it exists purely to read the retired database one last time.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from local_store import LocalPipelineStore
from markdown_store import (
    MarkdownMemoryStore,
    canonical_memory_id,
    format_history_date,
    humanize_key,
    render_history_line,
)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "memory.db"

# The legacy ``memories.memory_type`` taxonomy (semantic/episodic/procedural,
# mirroring RDB-style row classification) has no 1:1 successor in the new
# minimal frontmatter schema. It is remapped onto the new ``type``
# (profile/feedback/reference) taxonomy, which classifies a memory by how a
# human curating notes would use it rather than by its origin:
#
# - ``procedural`` (behavioural rules, e.g. "respond in Japanese") reads as
#   an instruction about how to collaborate -> ``feedback``.
# - ``semantic`` (long-lived facts/preferences, e.g. editor/OS/language) is
#   the textbook static-fact case -> ``profile``.
# - ``episodic`` (time-bound project state, e.g. the last command run) does
#   not fit either bucket well; it is the least-bad fit for ``reference``
#   (see ``llm-shared-memory-design.md`` for the caveat).
_MEMORY_TYPE_TO_TYPE = {
    "procedural": "feedback",
    "semantic": "profile",
    "episodic": "reference",
}


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_sessions(conn: sqlite3.Connection, local_store: LocalPipelineStore, apply: bool) -> int:
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    if apply:
        for row in rows:
            session = {
                "id": row["id"],
                "client": row["client"],
                "user_id": row["user_id"],
                "project_id": row["project_id"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "summary": row["summary"],
                "extracted_at": row["extracted_at"],
            }
            local_store.write_session(session)
    return len(rows)


def _migrate_events(conn: sqlite3.Connection, local_store: LocalPipelineStore, apply: bool) -> int:
    rows = conn.execute("SELECT * FROM events ORDER BY created_at ASC").fetchall()
    if apply:
        existing_by_session: dict[str, set[str]] = {}
        for row in rows:
            session_id = row["session_id"]
            if session_id not in existing_by_session:
                existing_by_session[session_id] = {
                    e["id"] for e in local_store.iter_events(session_id)
                }
            if row["id"] in existing_by_session[session_id]:
                continue
            local_store.append_event(
                session_id,
                role=row["role"],
                kind=row["kind"],
                content=row["content"],
                importance=row["importance"],
                event_id=row["id"],
            )
            existing_by_session[session_id].add(row["id"])
    return len(rows)


def _migrate_observations(conn: sqlite3.Connection, local_store: LocalPipelineStore, apply: bool) -> int:
    event_session: dict[str, str] = {
        row["id"]: row["session_id"] for row in conn.execute("SELECT id, session_id FROM events")
    }
    rows = conn.execute("SELECT * FROM observations ORDER BY observed_at ASC").fetchall()
    if apply:
        for row in rows:
            session_id = event_session.get(row["source_event_id"])
            if session_id is None:
                continue
            local_store.append_observation(
                session_id=session_id,
                source_event_id=row["source_event_id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                attribute=row["attribute"],
                value=json.loads(row["value_json"]),
                confidence=row["confidence"],
                scope=row["scope"],
                extractor_version=row["extractor_version"],
                observation_id=row["id"],
            )
    return len(rows)


_EVIDENCE_EXCERPT_LIMIT = 200
_EVIDENCE_OMITTED_NOTE = "（元データはログ形式のため省略）"
_LOG_LIKE_PREFIXES = ("<", "{", "[")
_LOG_LIKE_LINE_LENGTH = 80


def _looks_like_log_or_dump(evidence: str) -> bool:
    """Heuristically detect legacy ``evidence`` that is a raw log/structured
    dump rather than a short user quote (see ``_value_summary``).

    A curated Vault note should read as prose; a shell prompt transcript or
    an agent notification blob dropped in verbatim (even truncated to
    ``_EVIDENCE_EXCERPT_LIMIT`` characters) reads as noise, not evidence.
    This intentionally errs on simple, cheap signals rather than a full
    log-format parser:

    - starts with a tag/structured-data opener (``<...>``, ``{...}``, ``[...]``)
    - spans multiple lines with at least one line longer than a normal
      sentence (``_LOG_LIKE_LINE_LENGTH`` chars) -- typical of captured
      CLI/tool transcripts
    """
    stripped = evidence.strip()
    if not stripped:
        return False
    if stripped.startswith(_LOG_LIKE_PREFIXES):
        return True
    lines = stripped.splitlines()
    return len(lines) > 1 and any(len(line) > _LOG_LIKE_LINE_LENGTH for line in lines)


def _value_summary(value: dict[str, Any]) -> str:
    """Render a legacy ``value_json`` dict as body prose for the new file-based schema.

    The legacy schema kept ``summary`` (an already-written Japanese sentence)
    separately from ``value_json`` (the structured ``{value, evidence,
    source, category}`` the sentence was built from). The new schema has no
    structured value field -- everything lives in the body's prose -- so a
    migrated file uses the legacy ``summary`` column as its body verbatim
    (see ``_migrate_memories``); this helper only renders a compact evidence
    excerpt appended to that prose when the legacy row carried evidence
    beyond the plain value, so that information is not silently dropped.

    The excerpt is capped rather than embedded verbatim: some legacy rows'
    ``evidence`` is not a short user quote but an entire captured tool/CLI
    transcript (observed in real data up to tens of KB), and dumping that
    wholesale into a "curated note" would defeat the redesign's purpose
    (see ``llm-shared-memory-design.md``'s frontmatter-minimization section).
    """
    evidence = value.get("evidence")
    if not evidence or evidence == value.get("value"):
        return ""
    raw_evidence = str(evidence)
    if _looks_like_log_or_dump(raw_evidence):
        return f"\n\n根拠: {_EVIDENCE_OMITTED_NOTE}"
    normalized = " ".join(raw_evidence.split())
    if len(normalized) > _EVIDENCE_EXCERPT_LIMIT:
        normalized = f"{normalized[:_EVIDENCE_EXCERPT_LIMIT]}..."
    return f"\n\n根拠: {normalized}"


def _migrate_memories(conn: sqlite3.Connection, markdown_store: MarkdownMemoryStore, apply: bool) -> int:
    """Migrate the legacy ``memories`` table, one file per logical key.

    The legacy schema stored one row per version of a memory (``active`` plus
    any number of ``superseded``/``deleted`` rows for the same logical key).
    The Markdown store instead models a logical key as a single living
    document, so rows sharing the same logical key (entity_type + entity_id +
    key + scope + project_id) are grouped here: the chronologically latest
    row (by ``valid_from``) becomes the file's current frontmatter/summary,
    and every earlier row is folded into a ``## 変更履歴`` line via
    ``render_history_line``.
    """
    memory_rows = conn.execute("SELECT * FROM memories").fetchall()

    if not apply:
        return len(memory_rows)

    groups: dict[tuple[str, str, str, str, str | None], list[sqlite3.Row]] = {}
    for row in memory_rows:
        group_key = (row["entity_type"], row["entity_id"], row["key"], row["scope"], row["project_id"])
        groups.setdefault(group_key, []).append(row)

    for (entity_type, entity_id, key, scope, project_id), rows in groups.items():
        # Sort by valid_from, then valid_until (falling back to updated_at
        # for the still-active row, whose valid_until is NULL), then id as a
        # final deterministic tiebreaker. valid_from alone does not
        # distinguish rows recorded in the same instant (e.g. several
        # observations superseded together), so relying on it alone lets the
        # random UUID id decide the order -- producing a non-chronological
        # history. valid_until reflects when each value stopped being
        # current, so ordering by it keeps history entries chronological
        # even when valid_from ties, while staying idempotent across reruns.
        ordered_rows = sorted(
            rows,
            key=lambda r: (r["valid_from"], r["valid_until"] or r["updated_at"], r["id"]),
        )
        *history_rows, current_row = ordered_rows

        summaries = [
            f"{row['summary']}{_value_summary(json.loads(row['value_json']))}" for row in ordered_rows
        ]
        # A row whose summary is identical to the one immediately before it
        # (e.g. re-observed without the underlying value actually changing)
        # documents no real transition, so it would only produce a no-op
        # "X → X に変更" line; skip it instead of recording it.
        history_lines = [
            render_history_line(
                summaries[index],
                summaries[index + 1],
                format_history_date(ordered_rows[index]["valid_until"] or ordered_rows[index]["updated_at"]),
            )
            for index in range(len(history_rows))
            if summaries[index] != summaries[index + 1]
        ]

        record_id = canonical_memory_id(entity_type, entity_id, key, scope, project_id)
        record = {
            "id": record_id,
            "type": _MEMORY_TYPE_TO_TYPE.get(current_row["memory_type"], "profile"),
            "created": format_history_date(ordered_rows[0]["created_at"]),
            "updated": format_history_date(current_row["updated_at"]),
            "title": humanize_key(key),
            "summary": summaries[-1],
            "history": history_lines,
        }
        markdown_store.write(record)

    return len(memory_rows)


def migrate(db_path: Path, vault_dir: Path, local_dir: Path, apply: bool) -> dict[str, Any]:
    """Migrate a legacy SQLite memory.db into the Vault + local file stores.

    Returns a summary dict with per-table counts. When ``apply`` is False
    (the default via the CLI), no files are written; counts describe what
    *would* be migrated.
    """
    conn = _connect_readonly(db_path)
    try:
        if apply:
            local_store = LocalPipelineStore(local_dir)
            markdown_store = MarkdownMemoryStore(vault_dir)
        else:
            local_store = None
            markdown_store = None

        if apply:
            sessions = _migrate_sessions(conn, local_store, apply)
            events = _migrate_events(conn, local_store, apply)
            observations = _migrate_observations(conn, local_store, apply)
            memories = _migrate_memories(conn, markdown_store, apply)
        else:
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            observations = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()

    return {
        "applied": apply,
        "sessions": sessions,
        "events": events,
        "observations": observations,
        "memories": memories,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate the legacy SQLite memory.db into the Vault + local file stores."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to the legacy memory.db")
    parser.add_argument("--vault", type=Path, required=True, help="Target Vault directory")
    parser.add_argument("--local-dir", type=Path, required=True, help="Target local pipeline directory")
    parser.add_argument(
        "--apply", action="store_true", help="Write files (default is a dry run that only reports counts)"
    )
    args = parser.parse_args()

    summary = migrate(args.db.expanduser(), args.vault.expanduser(), args.local_dir.expanduser(), args.apply)
    json.dump({"ok": True, **summary}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
