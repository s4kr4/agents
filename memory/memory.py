#!/usr/bin/env python3
"""Shared memory CLI for local LLM environments.

Storage is split into two file-based layers (no SQLite):

- Vault (``MarkdownMemoryStore``): stable ``memories``, synced across machines
  via Syncthing (see ``LLM_MEMORY_VAULT``).
- Local (``LocalPipelineStore``): ``sessions`` / ``events`` / ``observations``
  and audit logs, kept per-machine and never synced (see
  ``LLM_MEMORY_LOCAL_DIR``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_store import LocalPipelineStore, new_id, resolve_local_dir, utc_now
from markdown_store import MarkdownMemoryStore, humanize_key, resolve_vault_dir

EXTRACTOR_VERSION = "rule-based-v1"
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


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Memory frontmatter now stores date-only strings (e.g. "2026-08-01",
    # see MarkdownMemoryStore.today_date()), which fromisoformat() parses as
    # a naive datetime; treat those as UTC so recency_score() can compare
    # them against an aware "now".
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class ObservationCandidate:
    type: str
    entity_type: str
    entity_id: str
    attribute: str
    scope: str
    confidence: float
    value: dict[str, Any]


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


def cmd_init_db(args: argparse.Namespace) -> None:
    # Directory creation happens in the store constructors (see main()).
    print_json(
        {
            "ok": True,
            "db": str(args.db) if args.db else None,
            "vault": str(args.markdown_store.vault_dir),
            "local_dir": str(args.local_store.local_dir),
        }
    )


def cmd_start_session(args: argparse.Namespace) -> None:
    session_id = args.session_id or new_id("sess")
    args.local_store.ensure_session(
        session_id, client=args.client, user_id=args.user_id, project_id=args.project_id
    )
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
    args.local_store.ensure_session(
        args.session_id, client=args.client, user_id=args.user_id, project_id=args.project_id
    )
    event = args.local_store.append_event(
        args.session_id,
        role=args.role,
        kind=args.kind,
        content=args.content,
        importance=args.importance,
        event_id=args.event_id,
    )

    print_json(
        {
            "ok": True,
            "event": {
                "id": event["id"],
                "session_id": args.session_id,
                "role": args.role,
                "kind": args.kind,
            },
        }
    )


def summarize_session(local_store: LocalPipelineStore, session_id: str) -> str:
    events = sorted(local_store.iter_events(session_id), key=lambda e: e["created_at"])
    if not events:
        return "空のセッション"

    # Matches the legacy SQLite behaviour: the earliest 20 events are fetched,
    # and only the last 5 of that window are used for the excerpt.
    window = events[:20]
    parts: list[str] = []
    for event in window[-5:]:
        content = event["content"]
        if len(content) > 80:
            content = f"{content[:77]}..."
        parts.append(f"{event['role']}:{event['kind']}={content}")
    return " / ".join(parts)


def iter_events_for_extraction(
    local_store: LocalPipelineStore, session_id: str | None = None
) -> list[dict[str, Any]]:
    if session_id:
        sessions_by_id = {session_id: local_store.get_session(session_id)}
        events = local_store.iter_events(session_id)
    else:
        sessions_by_id = {s["id"]: s for s in local_store.list_sessions()}
        events = local_store.iter_all_events()

    enriched: list[dict[str, Any]] = []
    for event in events:
        session = sessions_by_id.get(event["session_id"])
        record = dict(event)
        record["user_id"] = session["user_id"] if session else None
        record["project_id"] = session.get("project_id") if session else None
        record["client"] = session["client"] if session else None
        enriched.append(record)

    enriched.sort(key=lambda e: e["created_at"])
    return enriched


def build_candidates(event: dict[str, Any]) -> list[ObservationCandidate]:
    content = event["content"]
    lowered = content.lower()
    candidates: list[ObservationCandidate] = []

    if event["role"] == "user":
        if "日本語" in content:
            candidates.append(
                ObservationCandidate(
                    type="feedback",
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
                    type="feedback",
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
                        type="profile",
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
                        type="profile",
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
                        type="profile",
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
                type="reference",
                entity_type="project",
                entity_id=event["project_id"] or "default",
                attribute="recent_command",
                scope="project",
                confidence=min(max(float(event["importance"]), 0.1), 1.0),
                value={"value": command_text, "source": "command_event"},
            )
        )

    # NOTE: kind=summary events are NOT converted to recent_summary observations.
    # Session summaries are already stored per-session and accessible via the
    # history command. Promoting them to memory caused unbounded growth because
    # the summary text changes every session, defeating deduplication.

    return candidates


def insert_observations_for_events(
    local_store: LocalPipelineStore, events: list[dict[str, Any]]
) -> list[str]:
    inserted: list[str] = []
    for event in events:
        for candidate in build_candidates(event):
            value = {"type": candidate.type, **candidate.value}
            observation = local_store.append_observation(
                session_id=event["session_id"],
                source_event_id=event["id"],
                entity_type=candidate.entity_type,
                entity_id=candidate.entity_id,
                attribute=candidate.attribute,
                value=value,
                confidence=candidate.confidence,
                scope=candidate.scope,
                extractor_version=EXTRACTOR_VERSION,
            )
            if observation is not None:
                inserted.append(observation["id"])
    return inserted


def cmd_extract(args: argparse.Namespace) -> None:
    events = iter_events_for_extraction(args.local_store, args.session_id)
    inserted = insert_observations_for_events(args.local_store, events)
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


def resolve_effective_project_id(
    *, scope: str, project_id: str | None, entity_type: str, entity_id: str
) -> str | None:
    """Resolve the ``project_id`` that ``upsert_from_observation`` will actually use.

    The explicit ``project_id`` (see ``LocalPipelineStore.append_observation``)
    takes priority; it covers the ``write-memory`` CLI path, where
    ``entity_type`` stays "user" and the project can't be recovered from
    ``entity_id``. Falling back to deriving it from ``entity_id`` preserves
    the pipeline-extracted path (``build_candidates``' ``entity_type="project"``
    observations), which never sets it explicitly.
    """
    if project_id is None and scope == "project" and entity_type == "project":
        return entity_id
    return project_id


def upsert_memory_from_observation(
    markdown_store: MarkdownMemoryStore, observation: dict[str, Any]
) -> str:
    """Consolidate a pipeline ``observation`` into the Vault.

    The observation layer keeps its own richer schema (confidence, evidence,
    session/event provenance) unchanged -- see ``llm-shared-memory-design.md``.
    Only the ``type`` classification and the rendered ``summary`` prose cross
    over into the Vault's minimal frontmatter/body; confidence-based scoring
    and source provenance are deliberately not carried into the curated
    Vault record (see ``MarkdownMemoryStore``'s module docstring).
    """
    value = observation["value"]
    record_type = value.get("type", "profile")
    project_id = resolve_effective_project_id(
        scope=observation["scope"],
        project_id=observation.get("project_id"),
        entity_type=observation["entity_type"],
        entity_id=observation["entity_id"],
    )
    summary = summarize_memory(observation["attribute"], value)

    record = markdown_store.upsert_from_observation(
        type=record_type,
        entity_type=observation["entity_type"],
        entity_id=observation["entity_id"],
        key=observation["attribute"],
        scope=observation["scope"],
        project_id=project_id,
        summary=summary,
    )
    return record["id"]


def safe_upsert_memory_from_observation(
    markdown_store: MarkdownMemoryStore, observation: dict[str, Any]
) -> str | None:
    """Consolidate one ``observation`` into the Vault, skipping poisoned records.

    A malformed observation (e.g. ``scope="project"`` with no resolvable
    ``project_id``) must not abort a batch consolidation that also contains
    valid observations for other entities -- see ``cmd_consolidate``,
    ``cmd_end_session`` and ``_flush_one_queue_item``. Such an observation is
    skipped with a warning on stderr instead of raising.
    """
    try:
        return upsert_memory_from_observation(markdown_store, observation)
    except ValueError as exc:
        print(
            f"warning: skipping observation {observation.get('id')!r}: {exc}",
            file=sys.stderr,
        )
        return None


def cmd_list_unextracted(args: argparse.Namespace) -> None:
    sessions = args.local_store.list_unextracted(args.limit)
    results = [
        {
            "id": s["id"],
            "project_id": s.get("project_id"),
            "started_at": s["started_at"],
            "ended_at": s.get("ended_at"),
            "summary": s.get("summary"),
        }
        for s in sessions
    ]
    print_json({"ok": True, "sessions": results, "count": len(results)})


def cmd_write_memory(args: argparse.Namespace) -> None:
    # Validate before any local-store writes: a scope="project" call that
    # can't resolve a project_id must fail fast without leaving behind an
    # event/observation record, otherwise a poisoned observation lingers in
    # the local store and later aborts batch consolidation (see
    # ``safe_upsert_memory_from_observation``).
    effective_project_id = resolve_effective_project_id(
        scope=args.scope,
        project_id=args.project_id,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
    )
    if args.scope == "project" and not effective_project_id:
        raise SystemExit(
            f"scope='project' requires a project_id (key={args.key!r}); refusing to "
            "silently write a global-scope file instead"
        )

    event = args.local_store.append_event(
        args.session_id,
        role="system",
        kind="llm-extract-source",
        content=json.dumps(
            {"key": args.key, "summary": args.summary, "type": args.memory_type},
            ensure_ascii=False,
        ),
        importance=0.9,
    )

    value = {"type": args.memory_type, "value": args.summary, "source": "claude_code_extract"}
    observation = args.local_store.append_observation(
        session_id=args.session_id,
        source_event_id=event["id"],
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        attribute=args.key,
        value=value,
        confidence=args.confidence,
        scope=args.scope,
        project_id=args.project_id,
        extractor_version="claude-code-v1",
    )

    upsert_memory_from_observation(args.markdown_store, observation)

    print_json({"ok": True, "observation_id": observation["id"], "event_id": event["id"]})


def cmd_mark_extracted(args: argparse.Namespace) -> None:
    updated = args.local_store.mark_extracted(args.session_id)
    print_json({"ok": True, "updated": updated})


def cmd_end_session(args: argparse.Namespace) -> None:
    session = args.local_store.get_session(args.session_id)
    if not session:
        raise SystemExit(f"session not found: {args.session_id}")

    summary = args.summary or summarize_session(args.local_store, args.session_id)
    now = utc_now()
    args.local_store.update_session(args.session_id, ended_at=now, summary=summary)

    if args.append_summary_event:
        args.local_store.append_event(
            args.session_id, role="assistant", kind="summary", content=summary, importance=0.9
        )

    extracted_count = 0
    if args.extract:
        events = iter_events_for_extraction(args.local_store, args.session_id)
        extracted_count = len(insert_observations_for_events(args.local_store, events))

    consolidated_count = 0
    if args.consolidate:
        observations = sorted(
            args.local_store.iter_observations(args.session_id), key=lambda o: o["observed_at"]
        )
        for observation in observations:
            if safe_upsert_memory_from_observation(args.markdown_store, observation) is not None:
                consolidated_count += 1

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


def cmd_consolidate(args: argparse.Namespace) -> None:
    observations = sorted(
        args.local_store.iter_all_observations(), key=lambda o: o["observed_at"]
    )
    if args.entity_id:
        observations = [o for o in observations if o["entity_id"] == args.entity_id]
    if args.attribute:
        observations = [o for o in observations if o["attribute"] == args.attribute]

    memory_ids = [
        memory_id
        for memory_id in (
            safe_upsert_memory_from_observation(args.markdown_store, o) for o in observations
        )
        if memory_id is not None
    ]
    print_json({"ok": True, "memory_ids": memory_ids, "count": len(memory_ids)})


def score_memory(record: dict[str, Any], query: str | None) -> float:
    """Rank by recency plus query match.

    There is no confidence/salience score left to blend in here: curation
    already happened when the memory was written (see
    ``llm-shared-memory-design.md``), so retrieval trusts that judgment
    rather than re-deriving a score from stored numbers.
    """
    score = recency_score(record["updated"])
    if query:
        haystack = f"{record['title']} {record['summary']}".lower()
        for token in query.lower().split():
            if token in haystack:
                score += 0.2
    return score


def serialize_memory(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "type": record["type"],
        "title": record["title"],
        "summary": record["summary"],
        "scope": record["scope"],
        "project_id": record.get("project_id"),
        "entity_id": record.get("entity_id"),
        "updated": record["updated"],
    }


def score_history_memory(row: dict[str, Any], query: str | None) -> float:
    match_score = text_match_score([row["title"], row["summary"]], query)
    return recency_score(row["updated"]) * 0.6 + min(match_score, 6.0) * 0.4


def serialize_history_memory(row: dict[str, Any], query: str | None) -> dict[str, Any]:
    combined_text = " ".join([row["title"], row["summary"]])
    return {
        "kind": "memory",
        "id": row["id"],
        "type": row["type"],
        "scope": row["scope"],
        "title": row["title"],
        "summary": row["summary"],
        "project_id": row.get("project_id"),
        "entity_id": row.get("entity_id"),
        "updated": row["updated"],
        "history": row.get("history", []),
        "excerpt": make_excerpt(combined_text, query),
    }


def history_memory_match_score(row: dict[str, Any], query: str | None) -> float:
    return text_match_score([row["title"], row["summary"]], query)


def history_session_match_score(row: dict[str, Any], query: str | None) -> float:
    return text_match_score(
        [row.get("summary") or "", row.get("matched_event_content") or ""],
        query,
    )


def score_history_session(row: dict[str, Any], query: str | None) -> float:
    match_score = history_session_match_score(row, query)
    event_bonus = min(float(row["matched_event_count"]), 5.0) * 0.08
    return recency_score(row["started_at"]) * 0.45 + min(match_score, 6.0) * 0.4 + event_bonus


def serialize_history_session(row: dict[str, Any], query: str | None) -> dict[str, Any]:
    summary = row.get("summary") or "summary unavailable"
    excerpt_source = row.get("matched_event_content") or summary
    return {
        "kind": "session",
        "id": row["id"],
        "client": row["client"],
        "user_id": row["user_id"],
        "project_id": row.get("project_id"),
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "summary": summary,
        "matched_event_count": row["matched_event_count"],
        "excerpt": make_excerpt(excerpt_source, query),
    }


def score_history_event(row: dict[str, Any], query: str | None) -> float:
    match_score = history_event_match_score(row, query)
    importance = min(max(float(row["importance"]), 0.0), 1.0)
    return importance * 0.35 + recency_score(row["created_at"]) * 0.25 + min(match_score, 6.0) * 0.4


def serialize_history_event(row: dict[str, Any], query: str | None) -> dict[str, Any]:
    return {
        "kind": "event",
        "id": row["id"],
        "session_id": row["session_id"],
        "project_id": row.get("project_id"),
        "role": row["role"],
        "kind_name": row["kind"],
        "created_at": row["created_at"],
        "importance": row["importance"],
        "excerpt": make_excerpt(row["content"], query),
    }


def history_event_match_score(row: dict[str, Any], query: str | None) -> float:
    return text_match_score([row["content"]], query)


def dedupe_ranked_rows(
    rows: list[dict[str, Any]],
    key_name: str,
    scorer: Any,
    query: str | None,
) -> list[dict[str, Any]]:
    best_by_id: dict[str, tuple[float, dict[str, Any]]] = {}
    for row in rows:
        score = scorer(row, query)
        if query and score <= 0:
            continue
        row_id = row[key_name]
        existing = best_by_id.get(row_id)
        if existing is None or score > existing[0]:
            best_by_id[row_id] = (score, row)
    return [item[1] for item in sorted(best_by_id.values(), key=lambda item: item[0], reverse=True)]


def _history_memory_rows(
    markdown_store: MarkdownMemoryStore,
    project_id: str | None,
    entity_id: str | None,
    memory_type: str | None,
) -> list[dict[str, Any]]:
    """Return memory records matching the given filters.

    Earlier revisions joined each memory against the pipeline layer's
    session/event that produced it (``sources``). Frontmatter no longer
    tracks that provenance (see ``MarkdownMemoryStore``'s module docstring),
    so this is now a direct filter over the Vault with no pipeline lookups.
    """
    records = markdown_store.iter_all()
    if project_id:
        records = [r for r in records if r.get("project_id") == project_id]
    if entity_id:
        records = [r for r in records if r.get("entity_id") == entity_id]
    if memory_type:
        records = [r for r in records if r["type"] == memory_type]
    return records


def cmd_history(args: argparse.Namespace) -> None:
    local_store: LocalPipelineStore = args.local_store
    markdown_store: MarkdownMemoryStore = args.markdown_store
    query = args.query.strip() if args.query else None
    memory_hits: list[dict[str, Any]] = []
    session_hits: list[dict[str, Any]] = []
    event_hits: list[dict[str, Any]] = []
    returned_memory_ids: list[str] = []

    if args.include_memories:
        memory_rows = _history_memory_rows(
            markdown_store, args.project_id, args.entity_id, args.memory_type
        )
        ranked_memories = dedupe_ranked_rows(
            [row for row in memory_rows if not query or history_memory_match_score(row, query) > 0],
            "id",
            score_history_memory,
            query,
        )[: args.limit]
        memory_hits = [serialize_history_memory(row, query) for row in ranked_memories]
        returned_memory_ids.extend(hit["id"] for hit in memory_hits)

    if args.include_sessions:
        session_rows = []
        for session in local_store.list_sessions():
            if args.project_id and session.get("project_id") != args.project_id:
                continue
            if args.user_id and session.get("user_id") != args.user_id:
                continue
            events = local_store.iter_events(session["id"])
            row = dict(session)
            row["matched_event_count"] = len(events)
            row["matched_event_content"] = " || ".join(e["content"] for e in events)
            session_rows.append(row)

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
        event_rows = []
        for session in local_store.list_sessions():
            if args.project_id and session.get("project_id") != args.project_id:
                continue
            if args.user_id and session.get("user_id") != args.user_id:
                continue
            if args.entity_id and session.get("user_id") != args.entity_id:
                continue
            for event in local_store.iter_events(session["id"]):
                if args.role and event["role"] != args.role:
                    continue
                if args.kind and event["kind"] != args.kind:
                    continue
                row = dict(event)
                row["project_id"] = session.get("project_id")
                event_rows.append(row)

        ranked_events = sorted(
            [row for row in event_rows if not query or history_event_match_score(row, query) > 0],
            key=lambda row: score_history_event(row, query),
            reverse=True,
        )[: args.limit]
        event_hits = [serialize_history_event(row, query) for row in ranked_events]

    if args.session_id and returned_memory_ids:
        local_store.append_retrieval_log(
            session_id=args.session_id, query=query or "", returned_memory_ids=returned_memory_ids
        )

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
    records = args.markdown_store.search(
        query=args.query,
        entity_id=args.entity_id,
        type=args.memory_type,
        scope=args.scope,
        project_id=args.project_id,
    )
    ranked = sorted(records, key=lambda r: score_memory(r, args.query), reverse=True)[: args.limit]

    if args.session_id:
        args.local_store.append_retrieval_log(
            session_id=args.session_id,
            query=args.query or "",
            returned_memory_ids=[r["id"] for r in ranked],
        )

    print_json({"ok": True, "memories": [serialize_memory(r) for r in ranked], "count": len(ranked)})


def cmd_get_context(args: argparse.Namespace) -> None:
    """Return a response-context bundle, bucketed by ``type``.

    ``--user-id`` is accepted for CLI stability but no longer used: this
    store serves a single local default user and frontmatter no longer
    records an entity to filter global memories by (see
    ``MarkdownMemoryStore.get_context``).
    """
    records = args.markdown_store.get_context(project_id=args.project_id)

    payload: dict[str, list[dict[str, Any]]] = {"feedback": [], "profile": [], "reference": []}
    limits = {"feedback": 5, "profile": 10, "reference": 10}
    for record in records:
        bucket = record["type"]
        if bucket not in payload:
            continue
        if len(payload[bucket]) >= limits[bucket]:
            continue
        payload[bucket].append(serialize_memory(record))

    print_json({"ok": True, "context": payload})


def cmd_forget(args: argparse.Namespace) -> None:
    updated = args.markdown_store.forget(args.memory_id)
    args.local_store.append_deletion_log(
        target_type="memory", target_id=args.memory_id, reason=args.reason
    )
    print_json({"ok": True, "updated": updated, "memory_id": args.memory_id})


def cmd_queue_session(args: argparse.Namespace) -> None:
    """Save a complete session payload to a JSONL file (no store writes)."""
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


def _flush_one_queue_item(
    local_store: LocalPipelineStore, markdown_store: MarkdownMemoryStore, data: dict[str, Any]
) -> None:
    """Write a single queued session payload into the file-based stores."""
    session_id = data["session_id"]
    client = data.get("client", "unknown")
    user_id = data.get("user_id", "default")
    project_id = data.get("project_id")
    user_content = data.get("user_content", "")
    assistant_content = data.get("assistant_content", "")
    summary = data.get("summary", "")

    local_store.ensure_session(session_id, client=client, user_id=user_id, project_id=project_id)

    if user_content:
        local_store.append_event(session_id, role="user", kind="message", content=user_content, importance=0.5)

    if assistant_content:
        local_store.append_event(
            session_id, role="assistant", kind="message", content=assistant_content, importance=0.5
        )

    effective_summary = summary or f"session:{session_id}"
    local_store.update_session(session_id, ended_at=utc_now(), summary=effective_summary)
    local_store.append_event(
        session_id, role="assistant", kind="summary", content=effective_summary, importance=0.9
    )

    events = iter_events_for_extraction(local_store, session_id)
    insert_observations_for_events(local_store, events)

    observations = sorted(local_store.iter_observations(session_id), key=lambda o: o["observed_at"])
    for observation in observations:
        safe_upsert_memory_from_observation(markdown_store, observation)


def cmd_flush_queue(args: argparse.Namespace) -> None:
    """Read queued JSONL files and write their payloads into the file-based stores."""
    queue_dir = QUEUE_DIR
    files = sorted(queue_dir.glob("*.jsonl")) if queue_dir.exists() else []
    if not files:
        print_json({"ok": True, "flushed": 0})
        return

    flushed = 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            _flush_one_queue_item(args.local_store, args.markdown_store, data)
            f.unlink()
            flushed += 1
        except Exception:
            # Keep the file so it can be retried later
            continue

    print_json({"ok": True, "flushed": flushed})


def flush_queue_if_possible(vault_dir: Path, local_dir: Path) -> int:
    """Attempt to flush the queue to the file-based stores. Never raises."""
    try:
        queue_dir = QUEUE_DIR
        files = sorted(queue_dir.glob("*.jsonl")) if queue_dir.exists() else []
        if not files:
            return 0

        local_store = LocalPipelineStore(local_dir)
        markdown_store = MarkdownMemoryStore(vault_dir)
        flushed = 0
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                _flush_one_queue_item(local_store, markdown_store, data)
                f.unlink()
                flushed += 1
            except Exception:
                continue
        return flushed
    except Exception:
        return 0


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Clean up stale data: remove recent_summary memories/observations.

    Earlier revisions also deduplicated separate ``status="superseded"``
    memory files here, but that concept no longer exists: value changes are
    folded into the same file's ``## 変更履歴`` section instead of ever
    creating a separate file (see ``llm-shared-memory-design.md``), so
    ``deleted_duplicate_superseded`` is always ``0`` now. It is kept in the
    output for CLI contract stability (this command's output is outside the
    memories-schema redesign's allowed breakage, see the design doc).
    """
    markdown_store: MarkdownMemoryStore = args.markdown_store
    local_store: LocalPipelineStore = args.local_store

    recent_summary_title = humanize_key("recent_summary")
    summary_records = [r for r in markdown_store.iter_all() if r["title"] == recent_summary_title]
    deleted_summary = sum(1 for r in summary_records if markdown_store.delete(r["id"]))

    deleted_observations = 0
    for session in local_store.list_sessions():
        deleted_observations += local_store.remove_matching_observations(
            session["id"], attribute="recent_summary"
        )

    print_json(
        {
            "ok": True,
            "deleted_summary_memories": deleted_summary,
            "deleted_summary_observations": deleted_observations,
            "deleted_duplicate_superseded": 0,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared memory CLI for local LLM environments")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Deprecated, ignored. Storage is file-based; see --vault/--local-dir.",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Vault directory for stable memories (default: $LLM_MEMORY_VAULT)",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help="Local directory for the pipeline layer (default: $LLM_MEMORY_LOCAL_DIR)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize the vault/local directories")
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

    search = subparsers.add_parser("search", help="Search current (non-archived) memories")
    search.add_argument("--session-id")
    search.add_argument("--query")
    search.add_argument("--entity-id")
    search.add_argument("--memory-type", choices=["profile", "feedback", "reference"])
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
    history.add_argument("--memory-type", choices=["profile", "feedback", "reference"])
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

    forget = subparsers.add_parser("forget", help="Archive a memory (move it out of active results, never deleted)")
    forget.add_argument("--memory-id", required=True)
    forget.add_argument("--reason", required=True)
    forget.set_defaults(func=cmd_forget)

    queue_session = subparsers.add_parser(
        "queue-session", help="Save a session payload to a file-based queue (no store write)"
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
        "flush-queue", help="Flush queued session files into the file-based stores"
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
        "--memory-type", required=True, choices=["profile", "feedback", "reference"]
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
    args.db = args.db.expanduser() if args.db else None

    vault_dir, used_fallback = resolve_vault_dir(args.vault.expanduser() if args.vault else None)
    if used_fallback:
        print(
            f"warning: LLM_MEMORY_VAULT is not set; falling back to {vault_dir} "
            "(not synced by Syncthing)",
            file=sys.stderr,
        )
    local_dir = resolve_local_dir(args.local_dir.expanduser() if args.local_dir else None)

    args.markdown_store = MarkdownMemoryStore(vault_dir)
    args.local_store = LocalPipelineStore(local_dir)
    args.func(args)


if __name__ == "__main__":
    main()
