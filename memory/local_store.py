#!/usr/bin/env python3
"""Local (non-Vault, per-machine) pipeline store for sessions/events/observations/logs.

This directory is deliberately kept outside of the Syncthing-synced Vault:
raw pipeline data (append-only logs, in-flight sessions) is high-churn and
would otherwise cause unnecessary Syncthing traffic and conflict risk.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_config import resolve_local_dir as resolve_local_dir
from store_lock import locked, store_lock
from store_paths import StorePathError, checked_path, validate_component

DEFAULT_LOCAL_SUBDIR = Path(__file__).resolve().parent / "local"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class LocalPipelineStore:
    """File-based store for the pipeline layer (sessions/events/observations) and audit logs."""

    def __init__(self, local_dir: Path) -> None:
        self.local_dir = Path(local_dir).resolve()
        self.sessions_dir = self.local_dir / "sessions"
        self.events_dir = self.local_dir / "events"
        self.observations_dir = self.local_dir / "observations"
        self.logs_dir = self.local_dir / "logs"
        for directory in (self.sessions_dir, self.events_dir, self.observations_dir, self.logs_dir):
            checked_path(self.local_dir, directory)
            directory.mkdir(parents=True, exist_ok=True)

    def transaction(self, timeout: float = 30.0):
        return store_lock(self.local_dir, timeout)

    # -- atomic write helper -------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        tmp_path = path.with_name(f"{path.stem}.tmp-{uuid.uuid4().hex}{path.suffix}")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False))
            handle.write("\n")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
        return records

    # -- sessions --------------------------------------------------------

    def _session_path(self, session_id: str) -> Path:
        validate_component(session_id)
        return checked_path(self.local_dir, self.sessions_dir / f"{session_id}.json")

    @locked
    def ensure_session(
        self,
        session_id: str,
        *,
        client: str,
        user_id: str,
        project_id: str | None,
    ) -> dict[str, Any]:
        existing = self.get_session(session_id)
        if existing is not None:
            return existing

        session = {
            "id": session_id,
            "client": client,
            "user_id": user_id,
            "project_id": project_id,
            "started_at": utc_now(),
            "ended_at": None,
            "summary": None,
            "extracted_at": None,
        }
        self._atomic_write_json(self._session_path(session_id), session)
        return session

    @locked
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        path = self._session_path(session_id)
        if not path.exists():
            return None
        session = json.loads(path.read_text(encoding="utf-8"))
        if session.get("id") != session_id:
            raise StorePathError("stored session ID does not match its filename")
        return session

    @locked
    def write_session(self, session: dict[str, Any]) -> None:
        """Atomically write a full session record, overwriting any existing file.

        Unlike ``ensure_session``/``update_session`` (which build up a record
        incrementally), this accepts a complete record as-is. Used by the
        SQLite migration to preserve historical fields verbatim.
        """
        self._atomic_write_json(self._session_path(session["id"]), session)

    @locked
    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session is None:
            raise FileNotFoundError(f"session not found: {session_id}")
        if "id" in fields and fields["id"] != session_id:
            raise StorePathError("session ID cannot be changed")
        session.update(fields)
        self._atomic_write_json(self._session_path(session_id), session)
        return session

    @locked
    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = [
            self.get_session(path.stem) for path in sorted(self.sessions_dir.glob("*.json"))
        ]
        return sessions

    @locked
    def list_unextracted(self, limit: int = 10) -> list[dict[str, Any]]:
        candidates = [
            session
            for session in self.list_sessions()
            if session.get("extracted_at") is None and session.get("summary") is not None
        ]
        candidates.sort(key=lambda s: s.get("started_at") or "")
        return candidates[:limit]

    @locked
    def mark_extracted(self, session_id: str) -> int:
        session = self.get_session(session_id)
        if session is None or session.get("extracted_at") is not None:
            return 0
        self.update_session(session_id, extracted_at=utc_now())
        return 1

    # -- events ------------------------------------------------------------

    def _events_path(self, session_id: str) -> Path:
        validate_component(session_id)
        return checked_path(self.local_dir, self.events_dir / f"{session_id}.jsonl")

    @locked
    def append_event(
        self,
        session_id: str,
        *,
        role: str,
        kind: str,
        content: str,
        importance: float,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": event_id or new_id("evt"),
            "session_id": session_id,
            "role": role,
            "kind": kind,
            "content": content,
            "created_at": utc_now(),
            "importance": importance,
        }
        self._append_jsonl(self._events_path(session_id), event)
        return event

    @locked
    def iter_events(self, session_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._events_path(session_id))

    @locked
    def iter_all_events(self) -> list[dict[str, Any]]:
        events = []
        for path in sorted(self.events_dir.glob("*.jsonl")):
            events.extend(self.iter_events(path.stem))
        return events

    # -- observations --------------------------------------------------------

    def _observations_path(self, session_id: str) -> Path:
        validate_component(session_id)
        return checked_path(self.local_dir, self.observations_dir / f"{session_id}.jsonl")

    @locked
    def append_observation(
        self,
        *,
        session_id: str,
        source_event_id: str,
        entity_type: str,
        entity_id: str,
        attribute: str,
        value: dict[str, Any],
        confidence: float,
        scope: str,
        extractor_version: str,
        project_id: str | None = None,
        observation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Append a new observation, skipping it when the unique key already exists.

        Uniqueness mirrors the previous SQLite index:
        (source_event_id, entity_type, entity_id, attribute, extractor_version).

        ``project_id`` is optional and only needs to be passed explicitly
        when ``scope="project"`` and the project cannot be derived from
        ``entity_id`` (e.g. a manually written memory whose ``entity_type``
        is ``"user"``, not ``"project"``) -- see
        ``memory.upsert_memory_from_observation``.
        """
        existing = self.iter_observations(session_id)
        dedup_key = (source_event_id, entity_type, entity_id, attribute, extractor_version)
        for record in existing:
            record_key = (
                record["source_event_id"],
                record["entity_type"],
                record["entity_id"],
                record["attribute"],
                record["extractor_version"],
            )
            if record_key == dedup_key:
                return None

        observation = {
            "id": observation_id or new_id("obs"),
            "session_id": session_id,
            "source_event_id": source_event_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "attribute": attribute,
            "value": value,
            "confidence": confidence,
            "scope": scope,
            "project_id": project_id,
            "observed_at": utc_now(),
            "extractor_version": extractor_version,
        }
        self._append_jsonl(self._observations_path(session_id), observation)
        return observation

    @locked
    def iter_observations(self, session_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._observations_path(session_id))

    @locked
    def iter_all_observations(self) -> list[dict[str, Any]]:
        observations = []
        for path in sorted(self.observations_dir.glob("*.jsonl")):
            observations.extend(self.iter_observations(path.stem))
        return observations

    @locked
    def remove_matching_observations(self, session_id: str, *, attribute: str) -> int:
        """Remove observations with the given attribute from a session, atomically."""
        path = self._observations_path(session_id)
        existing = self._read_jsonl(path)
        keep = [record for record in existing if record["attribute"] != attribute]
        removed = len(existing) - len(keep)
        if removed == 0:
            return 0

        tmp_path = path.with_name(f"{path.stem}.tmp-{uuid.uuid4().hex}{path.suffix}")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in keep:
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")
        os.replace(tmp_path, path)
        return removed

    # -- audit logs --------------------------------------------------------

    @locked
    def append_retrieval_log(
        self,
        *,
        session_id: str | None,
        query: str | None,
        returned_memory_ids: list[str],
        log_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": log_id or new_id("ret"),
            "session_id": session_id,
            "query": query or "",
            "returned_memory_ids": returned_memory_ids,
            "created_at": utc_now(),
        }
        self._append_jsonl(checked_path(self.local_dir, self.logs_dir / "retrieval.jsonl"), entry)
        return entry

    @locked
    def append_deletion_log(
        self,
        *,
        target_type: str,
        target_id: str,
        reason: str,
        log_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": log_id or new_id("del"),
            "target_type": target_type,
            "target_id": target_id,
            "reason": reason,
            "created_at": utc_now(),
        }
        self._append_jsonl(checked_path(self.local_dir, self.logs_dir / "deletions.jsonl"), entry)
        return entry
