#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "memory.db"
EXTRACTOR_VERSION = "rule-based-v1"
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "memory" / "llm-shared-memory-schema.sql"
QUEUE_DIR = Path(
    os.environ.get(
        "LLM_MEMORY_QUEUE_DIR",
        str(Path.home() / ".cache" / "llm-memory" / "queue"),
    )
)
LANGUAGE_PREFERENCES = {
    "typescript": "TypeScript",
    "python": "Python",
    "rust": "Rust",
    "go": "Go",
}
EDITOR_PREFERENCES = {
    "neovim": "Neovim",
    "vim": "Vim",
    "vscode": "VSCode",
    "emacs": "Emacs",
}
OS_PREFERENCES = {
    "ubuntu": "Ubuntu",
    "macos": "macOS",
    "windows": "Windows",
    "arch": "Arch Linux",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class ObservationCandidate:
    memory_type: str
    entity_type: str
    entity_id: str
    attribute: str
    scope: str
    confidence: float
    value: dict[str, Any]


def connect_readwrite(db_path: Path) -> sqlite3.Connection:
    """Open a writable connection and run schema migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # /var/tmp/ は sandbox 環境でブロックされるため /tmp/ を使う（deprecated だが接続ごとに設定する唯一の方法）
    conn.execute("PRAGMA temp_store_directory = '/tmp'")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_schema(conn)
    ensure_search_index(conn)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection. Raises FileNotFoundError if DB is absent."""
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # /var/tmp/ は sandbox 環境でブロックされるため /tmp/ を使う（deprecated だが接続ごとに設定する唯一の方法）
    conn.execute("PRAGMA temp_store_directory = '/tmp'")
    return conn


# Backward compatibility alias
def connect(db_path: Path) -> sqlite3.Connection:
    return connect_readwrite(db_path)


def migrate_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_memories_active")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active
        ON memories(entity_type, entity_id, key, scope, COALESCE(project_id, ''))
        WHERE status = 'active'
        """
    )
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN extracted_at TEXT")
    except sqlite3.OperationalError:
        pass  # カラム既存


def ensure_search_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
        USING fts5(memory_id UNINDEXED, key, summary, value_text)
        """
    )
    conn.execute(
        """
        INSERT INTO memories_fts(memory_id, key, summary, value_text)
        SELECT id, key, summary, value_json
        FROM memories
        WHERE status = 'active'
          AND id NOT IN (SELECT memory_id FROM memories_fts)
        """
    )


def print_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def make_excerpt(text: str, query: str | None, limit: int = 220) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    if not query:
        return f"{normalized[: limit - 3]}..."

    lowered = normalized.lower()
    query_lower = query.lower()
    index = lowered.find(query_lower)
    if index == -1:
        return f"{normalized[: limit - 3]}..."

    start = max(0, index - limit // 3)
    end = min(len(normalized), start + limit)
    excerpt = normalized[start:end]
    if start > 0:
        excerpt = f"...{excerpt}"
    if end < len(normalized):
        excerpt = f"{excerpt}..."
    return excerpt


def text_match_score(texts: list[str], query: str | None) -> float:
    if not query:
        return 0.0

    haystacks = [text.lower() for text in texts]
    score = 0.0
    for token in query.lower().split():
        if not token:
            continue
        if any(token in haystack for haystack in haystacks):
            score += 1.0
    return score


def recency_score(timestamp: str | None) -> float:
    parsed = parse_timestamp(timestamp)
    if not parsed:
        return 0.0
    age_days = max((datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0, 0.0)
    return max(0.0, 1.0 - min(age_days / 180.0, 1.0))


def ensure_session(
    conn: sqlite3.Connection,
    session_id: str,
    client: str,
    user_id: str,
    project_id: str | None,
) -> None:
    row = conn.execute(
        "SELECT id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row:
        return

    conn.execute(
        """
        INSERT INTO sessions(id, client, user_id, project_id, started_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (session_id, client, user_id, project_id, utc_now()),
    )


def cmd_init_db(args: argparse.Namespace) -> None:
    connect(args.db).close()
    print_json({"ok": True, "db": str(args.db)})


def cmd_start_session(args: argparse.Namespace) -> None:
    session_id = args.session_id or new_id("sess")
    conn = connect(args.db)
    try:
        ensure_session(conn, session_id, args.client, args.user_id, args.project_id)
        conn.commit()
    finally:
        conn.close()
    print_json(
        {
            "ok": True,
            "session": {
                "id": session_id,
                "client": args.client,
                "user_id": args.user_id,
                "project_id": args.project_id,
            },
        }
    )


def cmd_append_event(args: argparse.Namespace) -> None:
    event_id = args.event_id or new_id("evt")
    conn = connect(args.db)
    try:
        ensure_session(conn, args.session_id, args.client, args.user_id, args.project_id)
        conn.execute(
            """
            INSERT INTO events(id, session_id, role, kind, content, created_at, importance)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                args.session_id,
                args.role,
                args.kind,
                args.content,
                utc_now(),
                args.importance,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print_json(
        {
            "ok": True,
            "event": {
                "id": event_id,
                "session_id": args.session_id,
                "role": args.role,
                "kind": args.kind,
            },
        }
    )


def summarize_session(conn: sqlite3.Connection, session_id: str) -> str:
    rows = conn.execute(
        """
        SELECT role, kind, content
        FROM events
        WHERE session_id = ?
        ORDER BY created_at ASC
        LIMIT 20
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        return "空のセッション"

    parts: list[str] = []
    for row in rows[-5:]:
        content = row["content"]
        if len(content) > 80:
            content = f"{content[:77]}..."
        parts.append(f"{row['role']}:{row['kind']}={content}")
    return " / ".join(parts)


def cmd_end_session(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        session = conn.execute(
            "SELECT id, ended_at, user_id, project_id FROM sessions WHERE id = ?",
            (args.session_id,),
        ).fetchone()
        if not session:
            raise SystemExit(f"session not found: {args.session_id}")

        summary = args.summary or summarize_session(conn, args.session_id)
        now = utc_now()
        conn.execute(
            """
            UPDATE sessions
            SET ended_at = ?, summary = ?
            WHERE id = ?
            """,
            (now, summary, args.session_id),
        )
        if args.append_summary_event:
            conn.execute(
                """
                INSERT INTO events(id, session_id, role, kind, content, created_at, importance)
                VALUES(?, ?, 'assistant', 'summary', ?, ?, ?)
                """,
                (new_id("evt"), args.session_id, summary, now, 0.9),
            )
        conn.commit()

        extracted_count = 0
        consolidated_count = 0
        if args.extract:
            events = iter_events_for_extraction(conn, args.session_id)
            extracted_count = len(insert_observations_for_events(conn, events))

        if args.consolidate:
            # Only process observations generated from events in the current session.
            # Processing all observations on every session end causes O(sessions × obs)
            # growth and re-supersedes memories that were already settled.
            rows = conn.execute(
                """
                SELECT o.*
                FROM observations o
                WHERE o.entity_id IN (?, ?)
                  AND o.source_event_id IN (
                    SELECT e.id FROM events e WHERE e.session_id = ?
                  )
                ORDER BY o.observed_at ASC
                """,
                (session["user_id"], session["project_id"] or "default", args.session_id),
            ).fetchall()
            for row in rows:
                upsert_memory_from_observation(conn, row)
                consolidated_count += 1

        conn.commit()
    finally:
        conn.close()

    print_json(
        {
            "ok": True,
            "session_id": args.session_id,
            "ended_at": now,
            "summary": summary,
            "extracted_count": extracted_count,
            "consolidated_count": consolidated_count,
        }
    )


def iter_events_for_extraction(conn: sqlite3.Connection, session_id: str | None) -> list[sqlite3.Row]:
    if session_id:
        rows = conn.execute(
            """
            SELECT e.*, s.user_id, s.project_id, s.client
            FROM events e
            JOIN sessions s ON s.id = e.session_id
            WHERE e.session_id = ?
            ORDER BY e.created_at ASC
            """,
            (session_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.*, s.user_id, s.project_id, s.client
            FROM events e
            JOIN sessions s ON s.id = e.session_id
            ORDER BY e.created_at ASC
            """
        ).fetchall()
    return rows


def build_candidates(event: sqlite3.Row) -> list[ObservationCandidate]:
    content = event["content"]
    lowered = content.lower()
    candidates: list[ObservationCandidate] = []

    if event["role"] == "user":
        if "日本語" in content:
            candidates.append(
                ObservationCandidate(
                    memory_type="procedural",
                    entity_type="user",
                    entity_id=event["user_id"],
                    attribute="response_language",
                    scope="global",
                    confidence=1.0,
                    value={
                        "value": "ja",
                        "evidence": content,
                        "source": "explicit_user_statement",
                    },
                )
            )

        if "english" in lowered or "英語" in content:
            candidates.append(
                ObservationCandidate(
                    memory_type="procedural",
                    entity_type="user",
                    entity_id=event["user_id"],
                    attribute="response_language",
                    scope="global",
                    confidence=0.9,
                    value={
                        "value": "en",
                        "evidence": content,
                        "source": "explicit_user_statement",
                    },
                )
            )

        for token, label in LANGUAGE_PREFERENCES.items():
            if token in lowered:
                candidates.append(
                    ObservationCandidate(
                        memory_type="semantic",
                        entity_type="user",
                        entity_id=event["user_id"],
                        attribute="preferred_language_runtime",
                        scope="global",
                        confidence=0.75,
                        value={
                            "value": label,
                            "evidence": content,
                            "source": "explicit_user_statement",
                            "category": "language",
                        },
                    )
                )

        for token, label in EDITOR_PREFERENCES.items():
            if token in lowered:
                candidates.append(
                    ObservationCandidate(
                        memory_type="semantic",
                        entity_type="user",
                        entity_id=event["user_id"],
                        attribute="preferred_editor",
                        scope="global",
                        confidence=0.7,
                        value={
                            "value": label,
                            "evidence": content,
                            "source": "explicit_user_statement",
                            "category": "editor",
                        },
                    )
                )

        for token, label in OS_PREFERENCES.items():
            if token in lowered:
                candidates.append(
                    ObservationCandidate(
                        memory_type="semantic",
                        entity_type="user",
                        entity_id=event["user_id"],
                        attribute="primary_os",
                        scope="global",
                        confidence=0.7,
                        value={
                            "value": label,
                            "evidence": content,
                            "source": "explicit_user_statement",
                            "category": "os",
                        },
                    )
                )

    if event["kind"] == "command":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {"raw": content}

        command_text = parsed["command"] if isinstance(parsed, dict) and "command" in parsed else content
        candidates.append(
            ObservationCandidate(
                memory_type="episodic",
                entity_type="project",
                entity_id=event["project_id"] or "default",
                attribute="recent_command",
                scope="project",
                confidence=min(max(float(event["importance"]), 0.1), 1.0),
                value={"value": command_text, "source": "command_event"},
            )
        )

    # NOTE: kind=summary events are NOT converted to recent_summary observations.
    # Session summaries are already stored in the events table (kind=summary) and
    # accessible via the history command. Promoting them to memory caused unbounded
    # growth because the summary text changes every session, defeating deduplication.

    return candidates


def insert_observations_for_events(
    conn: sqlite3.Connection, events: list[sqlite3.Row]
) -> list[str]:
    inserted: list[str] = []
    for event in events:
        for candidate in build_candidates(event):
            observation_id = new_id("obs")
            try:
                conn.execute(
                    """
                    INSERT INTO observations(
                        id, source_event_id, entity_type, entity_id, attribute,
                        value_json, confidence, scope, observed_at, extractor_version
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        event["id"],
                        candidate.entity_type,
                        candidate.entity_id,
                        candidate.attribute,
                        json.dumps(
                            {
                                "memory_type": candidate.memory_type,
                                **candidate.value,
                            },
                            ensure_ascii=False,
                        ),
                        candidate.confidence,
                        candidate.scope,
                        utc_now(),
                        EXTRACTOR_VERSION,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
            inserted.append(observation_id)
    return inserted


def cmd_extract(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        events = iter_events_for_extraction(conn, args.session_id)
        inserted = insert_observations_for_events(conn, events)
        conn.commit()
    finally:
        conn.close()

    print_json({"ok": True, "inserted_observation_ids": inserted, "count": len(inserted)})


def summarize_memory(key: str, value: dict[str, Any]) -> str:
    raw_value = value.get("value")
    if key == "response_language" and raw_value == "ja":
        return "応答は日本語で行う"
    if key == "response_language" and raw_value == "en":
        return "応答は英語で行う"
    if key == "preferred_language_runtime":
        return f"よく使う言語: {raw_value}"
    if key == "preferred_editor":
        return f"好みのエディタ: {raw_value}"
    if key == "primary_os":
        return f"主な OS: {raw_value}"
    if key == "recent_command":
        return f"最近実行したコマンド: {raw_value}"
    if key == "recent_summary":
        return f"最近の作業要約: {raw_value}"
    return f"{key}: {raw_value}"


def upsert_memory_from_observation(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    value = json.loads(row["value_json"])
    memory_type = value.get("memory_type", "semantic")
    project_id = row["entity_id"] if row["scope"] == "project" and row["entity_type"] == "project" else None
    existing_rows = conn.execute(
        """
        SELECT * FROM memories
        WHERE entity_type = ?
          AND entity_id = ?
          AND key = ?
          AND scope = ?
          AND COALESCE(project_id, '') = COALESCE(?, '')
          AND status = 'active'
        ORDER BY updated_at DESC, created_at DESC
        """,
        (row["entity_type"], row["entity_id"], row["attribute"], row["scope"], project_id),
    ).fetchall()

    now = utc_now()
    summary = summarize_memory(row["attribute"], value)
    matching_existing = next(
        (existing for existing in existing_rows if existing["value_json"] == row["value_json"]),
        None,
    )

    if matching_existing:
        new_confidence = min(1.0, max(matching_existing["confidence"], row["confidence"]) + 0.05)
        new_salience = min(1.0, max(matching_existing["salience"], row["confidence"]))
        conn.execute(
            """
            UPDATE memories
            SET confidence = ?, salience = ?, summary = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_confidence, new_salience, summary, now, matching_existing["id"]),
        )
        memory_id = matching_existing["id"]
        duplicate_ids = [
            existing["id"]
            for existing in existing_rows
            if existing["id"] != matching_existing["id"]
        ]
        if duplicate_ids:
            placeholders = ", ".join("?" for _ in duplicate_ids)
            conn.execute(
                f"""
                UPDATE memories
                SET status = 'superseded', valid_until = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, now, *duplicate_ids),
            )
            conn.executemany(
                "DELETE FROM memories_fts WHERE memory_id = ?",
                [(memory_id_to_delete,) for memory_id_to_delete in duplicate_ids],
            )
    else:
        if existing_rows:
            existing_ids = [existing["id"] for existing in existing_rows]
            placeholders = ", ".join("?" for _ in existing_ids)
            conn.execute(
                f"""
                UPDATE memories
                SET status = 'superseded', valid_until = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, now, *existing_ids),
            )
            conn.executemany(
                "DELETE FROM memories_fts WHERE memory_id = ?",
                [(memory_id_to_delete,) for memory_id_to_delete in existing_ids],
            )
        memory_id = new_id("mem")
        conn.execute(
            """
            INSERT INTO memories(
                id, memory_type, entity_type, entity_id, key, value_json, summary,
                confidence, salience, scope, project_id, status, valid_from,
                valid_until, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?)
            """,
            (
                memory_id,
                memory_type,
                row["entity_type"],
                row["entity_id"],
                row["attribute"],
                row["value_json"],
                summary,
                row["confidence"],
                row["confidence"],
                row["scope"],
                project_id,
                now,
                now,
                now,
            ),
        )
    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
    conn.execute(
        """
        INSERT INTO memories_fts(memory_id, key, summary, value_text)
        VALUES(?, ?, ?, ?)
        """,
        (memory_id, row["attribute"], summary, row["value_json"]),
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO memory_sources(memory_id, observation_id, weight)
        VALUES(?, ?, ?)
        """,
        (memory_id, row["id"], row["confidence"]),
    )
    return memory_id


def fetch_unextracted_sessions(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM sessions
           WHERE extracted_at IS NULL AND summary IS NOT NULL
           ORDER BY started_at ASC LIMIT ?""",
        (limit,),
    ).fetchall()


def fetch_active_memories_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT key, summary FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
    ).fetchall()
    lines = [f"{row['key']}: {row['summary']}" for row in rows]
    return "\n".join(lines)


def cmd_list_unextracted(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        sessions = fetch_unextracted_sessions(conn, args.limit)
        results = []
        for s in sessions:
            results.append({
                "id": s["id"],
                "project_id": s["project_id"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "summary": s["summary"],
            })
        print_json({"ok": True, "sessions": results, "count": len(results)})
    finally:
        conn.close()


def cmd_write_memory(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        session_id = args.session_id
        now = utc_now()

        # 仮想イベントを events に挿入（FK 制約対応）
        event_id = new_id("evt")
        conn.execute(
            """INSERT INTO events(id, session_id, role, kind, content, created_at, importance)
               VALUES(?, ?, 'system', 'llm-extract-source', ?, ?, 0.9)""",
            (event_id, session_id, json.dumps({
                "key": args.key,
                "summary": args.summary,
                "memory_type": args.memory_type,
            }, ensure_ascii=False), now),
        )

        # observation を挿入
        obs_id = new_id("obs")
        value_json = json.dumps({
            "memory_type": args.memory_type,
            "value": args.summary,
            "source": "claude_code_extract",
        }, ensure_ascii=False)
        conn.execute(
            """INSERT INTO observations(id, source_event_id, entity_type, entity_id,
               attribute, value_json, confidence, scope, observed_at, extractor_version)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'claude-code-v1')""",
            (obs_id, event_id, args.entity_type, args.entity_id,
             args.key, value_json, args.confidence, args.scope, now),
        )

        # consolidate（upsert_memory_from_observation）
        obs_row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
        upsert_memory_from_observation(conn, obs_row)

        conn.commit()
        print_json({"ok": True, "observation_id": obs_id, "event_id": event_id})
    finally:
        conn.close()


def cmd_mark_extracted(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        now = utc_now()
        updated = conn.execute(
            "UPDATE sessions SET extracted_at = ? WHERE id = ? AND extracted_at IS NULL",
            (now, args.session_id),
        ).rowcount
        conn.commit()
        print_json({"ok": True, "updated": updated})
    finally:
        conn.close()


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Clean up stale data: remove recent_summary and deduplicate superseded memories.

    1. Remove all recent_summary memories/observations (no longer generated).
    2. For each key, keep only the most recent superseded memory per distinct value_json,
       removing older duplicates.
    """
    conn = connect(args.db)
    try:
        # --- Phase 1: recent_summary の全削除 ---
        summary_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM memories WHERE key = 'recent_summary'"
            ).fetchall()
        ]
        if summary_ids:
            ph = ", ".join("?" for _ in summary_ids)
            conn.execute(f"DELETE FROM memory_sources WHERE memory_id IN ({ph})", summary_ids)
            conn.execute(f"DELETE FROM memories_fts WHERE memory_id IN ({ph})", summary_ids)

        deleted_summary = conn.execute(
            "DELETE FROM memories WHERE key = 'recent_summary'"
        ).rowcount

        deleted_observations = conn.execute(
            "DELETE FROM observations WHERE attribute = 'recent_summary'"
        ).rowcount

        # --- Phase 2: superseded の重複削除 ---
        # 同じ (key, entity_id, value_json) の superseded が複数あれば最新1件だけ残す
        dup_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT m.id FROM memories m
                WHERE m.status = 'superseded'
                  AND m.id NOT IN (
                    SELECT id FROM (
                      SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY key, entity_id, value_json
                        ORDER BY updated_at DESC
                      ) AS rn
                      FROM memories
                      WHERE status = 'superseded'
                    ) WHERE rn = 1
                  )
                """
            ).fetchall()
        ]
        if dup_ids:
            ph = ", ".join("?" for _ in dup_ids)
            conn.execute(f"DELETE FROM memory_sources WHERE memory_id IN ({ph})", dup_ids)
            conn.execute(f"DELETE FROM memories_fts WHERE memory_id IN ({ph})", dup_ids)
            conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", dup_ids)
        deleted_duplicates = len(dup_ids)

        conn.commit()
    finally:
        conn.close()

    print_json(
        {
            "ok": True,
            "deleted_summary_memories": deleted_summary,
            "deleted_summary_observations": deleted_observations,
            "deleted_duplicate_superseded": deleted_duplicates,
        }
    )


def cmd_consolidate(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    memory_ids: list[str] = []
    try:
        params: list[Any] = []
        conditions = ["1 = 1"]
        if args.entity_id:
            conditions.append("o.entity_id = ?")
            params.append(args.entity_id)
        if args.attribute:
            conditions.append("o.attribute = ?")
            params.append(args.attribute)

        rows = conn.execute(
            f"""
            SELECT o.*
            FROM observations o
            WHERE {' AND '.join(conditions)}
            ORDER BY o.observed_at ASC
            """,
            params,
        ).fetchall()
        for row in rows:
            memory_ids.append(upsert_memory_from_observation(conn, row))
        conn.commit()
    finally:
        conn.close()

    print_json({"ok": True, "memory_ids": memory_ids, "count": len(memory_ids)})


def score_memory(row: sqlite3.Row, query: str | None) -> float:
    score = row["confidence"] * 0.6 + row["salience"] * 0.4
    if query:
        haystack = f"{row['key']} {row['summary']} {row['value_json']}".lower()
        for token in query.lower().split():
            if token in haystack:
                score += 0.2
    return score


def serialize_memory(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "memory_type": row["memory_type"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "key": row["key"],
        "value": json.loads(row["value_json"]),
        "summary": row["summary"],
        "confidence": row["confidence"],
        "salience": row["salience"],
        "scope": row["scope"],
        "project_id": row["project_id"],
        "updated_at": row["updated_at"],
    }


def score_history_memory(row: sqlite3.Row, query: str | None) -> float:
    match_score = text_match_score(
        [row["key"], row["summary"], row["value_json"]],
        query,
    )
    status_penalty = 0.0 if row["status"] == "active" else 0.15
    return (
        row["confidence"] * 0.35
        + row["salience"] * 0.2
        + recency_score(row["updated_at"]) * 0.2
        + min(match_score, 6.0) * 0.25
        - status_penalty
    )


def serialize_history_memory(row: sqlite3.Row, query: str | None) -> dict[str, Any]:
    value = json.loads(row["value_json"])
    combined_text = " ".join(
        [
            row["summary"],
            row["value_json"],
            row["source_summary"] or "",
            row["source_excerpt"] or "",
        ]
    )
    return {
        "kind": "memory",
        "id": row["id"],
        "memory_type": row["memory_type"],
        "status": row["status"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "key": row["key"],
        "summary": row["summary"],
        "value": value,
        "project_id": row["project_id"],
        "updated_at": row["updated_at"],
        "source_session_id": row["source_session_id"],
        "source_event_id": row["source_event_id"],
        "source_summary": row["source_summary"],
        "excerpt": make_excerpt(combined_text, query),
    }


def history_memory_match_score(row: sqlite3.Row, query: str | None) -> float:
    return text_match_score(
        [
            row["key"],
            row["summary"],
            row["value_json"],
            row["source_summary"] or "",
            row["source_excerpt"] or "",
        ],
        query,
    )


def history_session_match_score(row: sqlite3.Row, query: str | None) -> float:
    return text_match_score(
        [row["summary"] or "", row["matched_event_content"] or ""],
        query,
    )


def score_history_session(row: sqlite3.Row, query: str | None) -> float:
    match_score = history_session_match_score(row, query)
    event_bonus = min(float(row["matched_event_count"]), 5.0) * 0.08
    return recency_score(row["started_at"]) * 0.45 + min(match_score, 6.0) * 0.4 + event_bonus


def serialize_history_session(row: sqlite3.Row, query: str | None) -> dict[str, Any]:
    summary = row["summary"] or "summary unavailable"
    excerpt_source = row["matched_event_content"] or summary
    return {
        "kind": "session",
        "id": row["id"],
        "client": row["client"],
        "user_id": row["user_id"],
        "project_id": row["project_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "summary": summary,
        "matched_event_count": row["matched_event_count"],
        "excerpt": make_excerpt(excerpt_source, query),
    }


def score_history_event(row: sqlite3.Row, query: str | None) -> float:
    match_score = history_event_match_score(row, query)
    importance = min(max(float(row["importance"]), 0.0), 1.0)
    return importance * 0.35 + recency_score(row["created_at"]) * 0.25 + min(match_score, 6.0) * 0.4


def serialize_history_event(row: sqlite3.Row, query: str | None) -> dict[str, Any]:
    return {
        "kind": "event",
        "id": row["id"],
        "session_id": row["session_id"],
        "project_id": row["project_id"],
        "role": row["role"],
        "kind_name": row["kind"],
        "created_at": row["created_at"],
        "importance": row["importance"],
        "excerpt": make_excerpt(row["content"], query),
    }


def history_event_match_score(row: sqlite3.Row, query: str | None) -> float:
    return text_match_score([row["content"]], query)


def dedupe_ranked_rows(
    rows: list[sqlite3.Row],
    key_name: str,
    scorer: Any,
    query: str | None,
) -> list[sqlite3.Row]:
    best_by_id: dict[str, tuple[float, sqlite3.Row]] = {}
    for row in rows:
        score = scorer(row, query)
        if query and score <= 0:
            continue
        row_id = row[key_name]
        existing = best_by_id.get(row_id)
        if existing is None or score > existing[0]:
            best_by_id[row_id] = (score, row)
    return [item[1] for item in sorted(best_by_id.values(), key=lambda item: item[0], reverse=True)]


def cmd_history(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        query = args.query.strip() if args.query else None
        memory_hits: list[dict[str, Any]] = []
        session_hits: list[dict[str, Any]] = []
        event_hits: list[dict[str, Any]] = []
        returned_memory_ids: list[str] = []

        if args.include_memories:
            memory_conditions = ["m.status != 'deleted'"]
            memory_params: list[Any] = []
            if args.project_id:
                memory_conditions.append("m.project_id = ?")
                memory_params.append(args.project_id)
            if args.entity_id:
                memory_conditions.append("m.entity_id = ?")
                memory_params.append(args.entity_id)
            if args.memory_type:
                memory_conditions.append("m.memory_type = ?")
                memory_params.append(args.memory_type)

            memory_rows = conn.execute(
                f"""
                SELECT
                    m.*,
                    e.id AS source_event_id,
                    e.session_id AS source_session_id,
                    e.content AS source_excerpt,
                    s.summary AS source_summary
                FROM memories m
                LEFT JOIN memory_sources ms ON ms.memory_id = m.id
                LEFT JOIN observations o ON o.id = ms.observation_id
                LEFT JOIN events e ON e.id = o.source_event_id
                LEFT JOIN sessions s ON s.id = e.session_id
                WHERE {' AND '.join(memory_conditions)}
                ORDER BY m.updated_at DESC
                """,
                memory_params,
            ).fetchall()

            ranked_memories = dedupe_ranked_rows(
                [
                    row
                    for row in memory_rows
                    if not query or history_memory_match_score(row, query) > 0
                ],
                "id",
                score_history_memory,
                query,
            )[: args.limit]
            memory_hits = [serialize_history_memory(row, query) for row in ranked_memories]
            returned_memory_ids.extend(hit["id"] for hit in memory_hits)

        if args.include_sessions:
            session_conditions = ["1 = 1"]
            session_params: list[Any] = []
            if args.project_id:
                session_conditions.append("s.project_id = ?")
                session_params.append(args.project_id)
            if args.user_id:
                session_conditions.append("s.user_id = ?")
                session_params.append(args.user_id)

            session_rows = conn.execute(
                f"""
                SELECT
                    s.*,
                    COUNT(e.id) AS matched_event_count,
                    GROUP_CONCAT(e.content, ' || ') AS matched_event_content
                FROM sessions s
                LEFT JOIN events e ON e.session_id = s.id
                WHERE {' AND '.join(session_conditions)}
                GROUP BY s.id
                ORDER BY s.started_at DESC
                """,
                session_params,
            ).fetchall()

            ranked_sessions = sorted(
                [
                    row
                    for row in session_rows
                    if not query or history_session_match_score(row, query) > 0
                ],
                key=lambda row: score_history_session(row, query),
                reverse=True,
            )[: args.limit]
            session_hits = [serialize_history_session(row, query) for row in ranked_sessions]

        if args.include_events:
            event_conditions = ["1 = 1"]
            event_params: list[Any] = []
            if args.project_id:
                event_conditions.append("s.project_id = ?")
                event_params.append(args.project_id)
            if args.user_id:
                event_conditions.append("s.user_id = ?")
                event_params.append(args.user_id)
            if args.entity_id:
                event_conditions.append("s.user_id = ?")
                event_params.append(args.entity_id)
            if args.role:
                event_conditions.append("e.role = ?")
                event_params.append(args.role)
            if args.kind:
                event_conditions.append("e.kind = ?")
                event_params.append(args.kind)

            event_rows = conn.execute(
                f"""
                SELECT e.*, s.project_id
                FROM events e
                JOIN sessions s ON s.id = e.session_id
                WHERE {' AND '.join(event_conditions)}
                ORDER BY e.created_at DESC
                """,
                event_params,
            ).fetchall()
            ranked_events = sorted(
                [
                    row
                    for row in event_rows
                    if not query or history_event_match_score(row, query) > 0
                ],
                key=lambda row: score_history_event(row, query),
                reverse=True,
            )[: args.limit]
            event_hits = [serialize_history_event(row, query) for row in ranked_events]

        if args.session_id and returned_memory_ids:
            conn.execute(
                """
                INSERT INTO retrieval_logs(id, session_id, query, returned_memory_ids, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    new_id("ret"),
                    args.session_id,
                    query or "",
                    json.dumps(returned_memory_ids, ensure_ascii=False),
                    utc_now(),
                ),
            )
            conn.commit()
    finally:
        conn.close()

    print_json(
        {
            "ok": True,
            "query": query,
            "project_id": args.project_id,
            "memories": memory_hits,
            "sessions": session_hits,
            "events": event_hits,
            "counts": {
                "memories": len(memory_hits),
                "sessions": len(session_hits),
                "events": len(event_hits),
            },
        }
    )


def cmd_search(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        conditions = ["status = 'active'"]
        params: list[Any] = []

        if args.entity_id:
            conditions.append("entity_id = ?")
            params.append(args.entity_id)
        if args.memory_type:
            conditions.append("memory_type = ?")
            params.append(args.memory_type)
        if args.scope:
            conditions.append("scope = ?")
            params.append(args.scope)
        if args.project_id:
            conditions.append("(project_id = ? OR project_id IS NULL)")
            params.append(args.project_id)
        if args.query:
            like_value = f"%{args.query}%"
            conditions.append(
                """
                (
                    id IN (
                        SELECT memory_id
                        FROM memories_fts
                        WHERE memories_fts MATCH ?
                    )
                    OR key LIKE ?
                    OR summary LIKE ?
                    OR value_json LIKE ?
                )
                """
            )
            params.extend([args.query, like_value, like_value, like_value])

        rows = conn.execute(
            f"""
            SELECT memories.*
            FROM memories
            WHERE {' AND '.join(conditions)}
            ORDER BY memories.updated_at DESC
            """,
            params,
        ).fetchall()
        ranked = sorted(rows, key=lambda row: score_memory(row, args.query), reverse=True)[: args.limit]
        if args.session_id:
            conn.execute(
                """
                INSERT INTO retrieval_logs(id, session_id, query, returned_memory_ids, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    new_id("ret"),
                    args.session_id,
                    args.query or "",
                    json.dumps([row["id"] for row in ranked], ensure_ascii=False),
                    utc_now(),
                ),
            )
            conn.commit()
    finally:
        conn.close()

    print_json({"ok": True, "memories": [serialize_memory(row) for row in ranked], "count": len(ranked)})


def cmd_get_context(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM memories
            WHERE status = 'active'
              AND (
                (scope = 'global' AND entity_type = 'user' AND entity_id = ?)
                OR (scope = 'project' AND project_id = ?)
              )
            ORDER BY updated_at DESC
            """,
            (args.user_id, args.project_id),
        ).fetchall()
    finally:
        conn.close()

    payload = {"procedural": [], "semantic": [], "episodic": []}
    limits = {"procedural": 5, "semantic": 10, "episodic": 10}
    for row in rows:
        bucket = row["memory_type"]
        if bucket not in payload:
            continue
        if len(payload[bucket]) >= limits[bucket]:
            continue
        payload[bucket].append(serialize_memory(row))

    print_json({"ok": True, "context": payload})


def cmd_forget(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE memories
            SET status = 'deleted', valid_until = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, now, args.memory_id),
        )
        conn.execute(
            """
            INSERT INTO deletions(id, target_type, target_id, reason, created_at)
            VALUES(?, 'memory', ?, ?, ?)
            """,
            (new_id("del"), args.memory_id, args.reason, now),
        )
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (args.memory_id,))
        conn.commit()
    finally:
        conn.close()

    print_json({"ok": True, "updated": updated.rowcount, "memory_id": args.memory_id})


def cmd_queue_session(args: argparse.Namespace) -> None:
    """Save a complete session payload to a JSONL file without touching SQLite."""
    queue_dir = QUEUE_DIR
    queue_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    fname = queue_dir / f"{ts}_{args.session_id[:16]}.jsonl"

    payload = {
        "session_id": args.session_id,
        "client": args.client,
        "user_id": args.user_id,
        "project_id": args.project_id,
        "user_content": args.user_content or "",
        "assistant_content": args.assistant_content or "",
        "summary": args.summary or "",
        "queued_at": utc_now(),
    }
    fname.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    print_json({"ok": True, "queued": str(fname)})


def _flush_one_queue_item(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    """Write a single queued session payload into the database."""
    session_id = data["session_id"]
    client = data.get("client", "unknown")
    user_id = data.get("user_id", "default")
    project_id = data.get("project_id")
    user_content = data.get("user_content", "")
    assistant_content = data.get("assistant_content", "")
    summary = data.get("summary", "")
    now = utc_now()

    ensure_session(conn, session_id, client, user_id, project_id)

    if user_content:
        conn.execute(
            """
            INSERT INTO events(id, session_id, role, kind, content, created_at, importance)
            VALUES(?, ?, 'user', 'message', ?, ?, ?)
            """,
            (new_id("evt"), session_id, user_content, now, 0.5),
        )

    if assistant_content:
        conn.execute(
            """
            INSERT INTO events(id, session_id, role, kind, content, created_at, importance)
            VALUES(?, ?, 'assistant', 'message', ?, ?, ?)
            """,
            (new_id("evt"), session_id, assistant_content, now, 0.5),
        )

    effective_summary = summary or f"session:{session_id}"
    conn.execute(
        """
        UPDATE sessions
        SET ended_at = ?, summary = ?
        WHERE id = ?
        """,
        (now, effective_summary, session_id),
    )
    conn.execute(
        """
        INSERT INTO events(id, session_id, role, kind, content, created_at, importance)
        VALUES(?, ?, 'assistant', 'summary', ?, ?, ?)
        """,
        (new_id("evt"), session_id, effective_summary, now, 0.9),
    )

    events = iter_events_for_extraction(conn, session_id)
    insert_observations_for_events(conn, events)

    session_row = conn.execute(
        "SELECT user_id, project_id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if session_row:
        obs_rows = conn.execute(
            """
            SELECT o.*
            FROM observations o
            WHERE o.entity_id IN (?, ?)
            ORDER BY o.observed_at ASC
            """,
            (session_row["user_id"], session_row["project_id"] or "default"),
        ).fetchall()
        for obs_row in obs_rows:
            upsert_memory_from_observation(conn, obs_row)


def cmd_flush_queue(args: argparse.Namespace) -> None:
    """Read queued JSONL files and write their payloads into the SQLite database."""
    queue_dir = QUEUE_DIR
    files = sorted(queue_dir.glob("*.jsonl")) if queue_dir.exists() else []
    if not files:
        print_json({"ok": True, "flushed": 0})
        return

    conn = connect_readwrite(args.db)
    flushed = 0
    try:
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                _flush_one_queue_item(conn, data)
                conn.commit()
                f.unlink()
                flushed += 1
            except Exception:
                # Keep the file so it can be retried later
                continue
    finally:
        conn.close()

    print_json({"ok": True, "flushed": flushed})


def flush_queue_if_possible(db_path: Path) -> int:
    """Attempt to flush the queue to DB if DB is writable. Never raises."""
    try:
        # Quick writability probe
        probe = sqlite3.connect(db_path)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        finally:
            probe.close()
    except Exception:
        return 0

    try:
        queue_dir = QUEUE_DIR
        files = sorted(queue_dir.glob("*.jsonl")) if queue_dir.exists() else []
        if not files:
            return 0

        conn = connect_readwrite(db_path)
        flushed = 0
        try:
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    _flush_one_queue_item(conn, data)
                    conn.commit()
                    f.unlink()
                    flushed += 1
                except Exception:
                    continue
        finally:
            conn.close()
        return flushed
    except Exception:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared memory CLI for local LLM environments")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize database schema")
    init_db.set_defaults(func=cmd_init_db)

    start_session = subparsers.add_parser("start-session", help="Create a session if needed")
    start_session.add_argument("--session-id")
    start_session.add_argument("--client", required=True)
    start_session.add_argument("--user-id", required=True)
    start_session.add_argument("--project-id")
    start_session.set_defaults(func=cmd_start_session)

    end_session = subparsers.add_parser("end-session", help="Close a session and store a summary")
    end_session.add_argument("--session-id", required=True)
    end_session.add_argument("--summary")
    end_session.add_argument("--append-summary-event", action="store_true")
    end_session.add_argument("--extract", action="store_true")
    end_session.add_argument("--consolidate", action="store_true")
    end_session.set_defaults(func=cmd_end_session)

    append_event = subparsers.add_parser("append-event", help="Append an event to a session")
    append_event.add_argument("--event-id")
    append_event.add_argument("--session-id", required=True)
    append_event.add_argument("--client", required=True)
    append_event.add_argument("--user-id", required=True)
    append_event.add_argument("--project-id")
    append_event.add_argument("--role", required=True)
    append_event.add_argument("--kind", required=True)
    append_event.add_argument("--content", required=True)
    append_event.add_argument("--importance", type=float, default=0.5)
    append_event.set_defaults(func=cmd_append_event)

    extract = subparsers.add_parser("extract", help="Extract observations from events")
    extract.add_argument("--session-id")
    extract.set_defaults(func=cmd_extract)

    consolidate = subparsers.add_parser("consolidate", help="Build memories from observations")
    consolidate.add_argument("--entity-id")
    consolidate.add_argument("--attribute")
    consolidate.set_defaults(func=cmd_consolidate)

    search = subparsers.add_parser("search", help="Search active memories")
    search.add_argument("--session-id")
    search.add_argument("--query")
    search.add_argument("--entity-id")
    search.add_argument("--memory-type", choices=["procedural", "semantic", "episodic"])
    search.add_argument("--scope", choices=["global", "project", "client", "temporary"])
    search.add_argument("--project-id")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    history = subparsers.add_parser("history", help="Search historical sessions, events, and memories")
    history.add_argument("--session-id")
    history.add_argument("--query")
    history.add_argument("--project-id")
    history.add_argument("--user-id")
    history.add_argument("--entity-id")
    history.add_argument("--memory-type", choices=["procedural", "semantic", "episodic"])
    history.add_argument("--role", choices=["user", "assistant", "system", "tool"])
    history.add_argument("--kind")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--include-memories", action="store_true", default=True)
    history.add_argument("--no-memories", dest="include_memories", action="store_false")
    history.add_argument("--include-sessions", action="store_true", default=True)
    history.add_argument("--no-sessions", dest="include_sessions", action="store_false")
    history.add_argument("--include-events", action="store_true", default=True)
    history.add_argument("--no-events", dest="include_events", action="store_false")
    history.set_defaults(func=cmd_history)

    get_context = subparsers.add_parser("get-context", help="Get response context bundle")
    get_context.add_argument("--user-id", required=True)
    get_context.add_argument("--project-id", required=True)
    get_context.set_defaults(func=cmd_get_context)

    forget = subparsers.add_parser("forget", help="Mark a memory as deleted")
    forget.add_argument("--memory-id", required=True)
    forget.add_argument("--reason", required=True)
    forget.set_defaults(func=cmd_forget)

    queue_session = subparsers.add_parser(
        "queue-session", help="Save a session payload to a file-based queue (no DB write)"
    )
    queue_session.add_argument("--session-id", required=True)
    queue_session.add_argument("--client", required=True)
    queue_session.add_argument("--user-id", required=True)
    queue_session.add_argument("--project-id")
    queue_session.add_argument("--user-content")
    queue_session.add_argument("--assistant-content")
    queue_session.add_argument("--summary")
    queue_session.set_defaults(func=cmd_queue_session)

    flush_queue = subparsers.add_parser(
        "flush-queue", help="Flush queued session files into the SQLite database"
    )
    flush_queue.set_defaults(func=cmd_flush_queue)

    cleanup = subparsers.add_parser(
        "cleanup", help="Remove stale recent_summary memories and observations"
    )
    cleanup.set_defaults(func=cmd_cleanup)

    list_unextracted = subparsers.add_parser(
        "list-unextracted", help="List sessions not yet extracted"
    )
    list_unextracted.add_argument("--limit", type=int, default=10)
    list_unextracted.set_defaults(func=cmd_list_unextracted)

    write_memory = subparsers.add_parser(
        "write-memory",
        help="Write an extracted memory (creates observation and consolidates)",
    )
    write_memory.add_argument("--session-id", required=True)
    write_memory.add_argument(
        "--memory-type", required=True, choices=["semantic", "episodic", "procedural"]
    )
    write_memory.add_argument("--entity-type", default="user")
    write_memory.add_argument("--entity-id", default="default")
    write_memory.add_argument("--key", required=True)
    write_memory.add_argument("--summary", required=True)
    write_memory.add_argument("--confidence", type=float, default=0.8)
    write_memory.add_argument("--scope", default="global", choices=["global", "project"])
    write_memory.add_argument("--project-id")
    write_memory.set_defaults(func=cmd_write_memory)

    mark_extracted = subparsers.add_parser(
        "mark-extracted", help="Mark a session as extracted"
    )
    mark_extracted.add_argument("--session-id", required=True)
    mark_extracted.set_defaults(func=cmd_mark_extracted)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.db = args.db.expanduser()
    args.func(args)


if __name__ == "__main__":
    main()
