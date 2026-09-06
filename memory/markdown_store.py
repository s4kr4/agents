#!/usr/bin/env python3
"""Markdown-based store for stable memories, synced across machines via Syncthing.

Each memory is a single Markdown file with a **minimal** YAML frontmatter
(``type`` / ``created`` / ``updated`` only -- see below). A file models a
*logical key* (entity_type + entity_id + key + scope + project_id) as a
living document: the body carries the current summary plus a
``## 変更履歴`` (change history) section that records earlier summaries as
plain lines, appended to in place as the content changes over time. There is
never more than one file per logical key -- this is intentionally modeled
after how a note-taking app (e.g. Obsidian) treats a topic, not after a
relational table's row-per-version history.

Files are grouped into directories by ``scope`` so that the Vault stays
legible as it grows. ``memory/`` itself contains only the generated index;
every memory Markdown file lives in a subdirectory:

- ``scope="global"`` (the common case: a user-level preference/fact) is kept
  under ``memory/global/``.
- ``scope="project"`` is grouped under ``memory/projects/<project_slug>/``.
- ``scope="client"`` is grouped under ``memory/clients/<entity_slug>/``.
- ``scope="temporary"`` is kept flat under ``memory/temporary/``.

Within a directory, the filename (and the record's ``id``, which is always
the file's path relative to ``memory/`` without the ``.md`` extension) is a
human-readable, kebab-case slug derived from the logical key, so that
Obsidian's file list / graph view and the Vault directory itself are legible
without opening a file. The slug is computed deterministically from the
logical key so that concurrent writers on different machines converge on the
same file for the same memory instead of creating diverging duplicates.

**Frontmatter is deliberately minimal.** Earlier revisions mirrored the
legacy SQLite ``memories`` table almost 1:1 (``id``/``entity_type``/
``entity_id``/``scope``/``project_id``/``confidence``/``salience``/
``status``/``valid_from``/``valid_until``/``sources``/``value`` as a nested
dict). Most of that duplicated information the directory layout already
encodes (``scope``/``project_id``/client ``entity_id``), or was an
algorithmic score meaningless to a human reading the Vault in Obsidian
(``confidence``/``salience``), or was an internal pipeline reference
(``sources``: session/event ids). This store now keeps only what a human
curating notes actually needs:

- ``type``: ``profile`` (静的事実) / ``feedback`` (協働のしかたの学び) /
  ``reference`` (外部システムへのポインタ)
- ``created`` / ``updated``: local ISO-8601 timestamps with seconds and UTC offset

Everything else -- the value itself, its evidence, its category -- lives in
the body's plain prose, where a human (or the LLM) can read it naturally.
See ``llm-shared-memory-design.md`` for the full rationale.

``forget()`` no longer flips a ``status: deleted`` flag; it physically moves
the file to ``memory/archive/<same relative path>`` (see ``forget()``). If
that destination is already occupied (the same logical key was forgotten
before, freeing its slug for reuse -- see ``_existing_ids()``), the new
archive file is renamed with a timestamp suffix instead of overwriting the
earlier one; the move is never a silent overwrite. Archived files are
excluded from ``iter_all()`` and ``_index.md``.

``memory/_index.md`` is a generated, write-only index of all (non-archived)
memories (grouped by scope), regenerated on every write so that the Vault
directory is browsable without a search tool.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from memory_config import resolve_vault_dir as resolve_vault_dir
from store_lock import locked, store_lock
from store_paths import StorePathError, checked_path, validate_memory_id

DEFAULT_VAULT_SUBDIR = Path(__file__).resolve().parent / "vault"
SYNC_CONFLICT_PATTERN = re.compile(r"\.sync-conflict-")
INDEX_FILENAME = "_index.md"
ARCHIVE_DIRNAME = "archive"
HISTORY_HEADING = "## 変更履歴"

_SLUG_INVALID_CHARS = re.compile(r"[^\w]+", re.UNICODE)
_SLUG_MULTI_DASH = re.compile(r"-+")
_KEY_WORD_SPLIT = re.compile(r"[_\-\s]+")

_TAG_WHITESPACE = re.compile(r"\s+")
_TAG_INVALID_CHARS = re.compile(r"[^\w/]+", re.UNICODE)
_TAG_MULTI_DASH = re.compile(r"-+")

# Canonical frontmatter key order: known fields first (matching write()'s
# layout), then any unrecognized keys in their original relative order. Used
# by update_metadata() to keep a stable, predictable diff when tags/related
# are added to a file that didn't have them yet (see the module's tags/
# related design notes).
_FRONTMATTER_CORE_ORDER = ("type", "created", "updated", "tags", "related")


def current_timestamp() -> str:
    """Return local ISO-8601 date/time with seconds and an explicit UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    """Convert text into a lowercase, kebab-case slug safe for filenames/links."""
    normalized = text.strip().lower().replace("_", "-")
    normalized = _SLUG_INVALID_CHARS.sub("-", normalized)
    normalized = _SLUG_MULTI_DASH.sub("-", normalized).strip("-")
    return normalized or "untitled"


def normalize_tag(value: str) -> str:
    """Normalize a single tag for writing: lowercase kebab-case.

    Unlike ``slugify()``, this preserves ``/`` (Obsidian's hierarchical-tag
    separator, e.g. ``env/wsl``) and raises instead of falling back to a
    placeholder when the result would be empty -- a silently generated
    meaningless tag is worse than a rejected one. Unicode word characters
    (including Japanese) are preserved. Known limitation: ``C++`` and ``C#``
    both normalize to ``c``; use ``cpp``/``csharp`` instead.
    """
    if not isinstance(value, str):
        raise ValueError(f"tag must be a string, got {value!r}")
    normalized = value.strip().lower().replace("_", "-")
    normalized = _TAG_WHITESPACE.sub("-", normalized)
    normalized = _TAG_INVALID_CHARS.sub("-", normalized)
    normalized = _TAG_MULTI_DASH.sub("-", normalized).strip("-")
    if not normalized:
        raise ValueError(f"tag normalizes to an empty string: {value!r}")
    return normalized


def normalize_tags(tags: Any) -> list[str]:
    """Validate and normalize a list of tags for writing (strict boundary).

    ``tags`` must be a list; each element must be a string that normalizes
    to a non-empty tag via ``normalize_tag()``. Duplicates (after
    normalization) are removed and the result is sorted so that repeated
    writes of the same tag set produce byte-identical frontmatter (helps
    Syncthing converge instead of flagging spurious conflicts).
    """
    if not isinstance(tags, list):
        raise ValueError(f"tags must be a list, got {tags!r}")
    return sorted({normalize_tag(tag) for tag in tags})


def normalize_related(related: Any, self_id: str | None = None) -> list[str]:
    """Validate and normalize a list of related memory ids for writing.

    ``related`` must be a list of memory-id-shaped strings, checked via
    ``validate_memory_id()`` (format only -- existence of the referenced
    memory is a read-time concern, see ``MarkdownMemoryStore.related()``,
    not a write-time one, since ``forget()`` legitimately produces dangling
    references). ``self_id`` (the record's own id, if known yet) and
    duplicates are silently removed; the result is sorted for the same
    determinism reason as ``normalize_tags()``.
    """
    if not isinstance(related, list):
        raise ValueError(f"related must be a list, got {related!r}")
    normalized: set[str] = set()
    for item in related:
        if not isinstance(item, str):
            raise ValueError(f"related id must be a string, got {item!r}")
        validate_memory_id(item)
        if item == self_id:
            continue
        normalized.add(item)
    return sorted(normalized)


def _lenient_tags(raw: Any, memory_id: str) -> list[str]:
    """Read frontmatter ``tags`` tolerantly: warn and drop what's malformed
    instead of failing the whole read (see the module's tags/related design
    notes -- lenient reads are a deliberately different boundary from the
    strict ``normalize_tags()`` used when writing).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        print(
            f"warning: {memory_id}: ignoring invalid 'tags' value (expected a list): {raw!r}",
            file=sys.stderr,
        )
        return []

    normalized: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            print(f"warning: {memory_id}: ignoring invalid tag element: {item!r}", file=sys.stderr)
            continue
        try:
            normalized.add(normalize_tag(item))
        except ValueError:
            print(f"warning: {memory_id}: ignoring invalid tag element: {item!r}", file=sys.stderr)
    return sorted(normalized)


def _lenient_related(raw: Any, memory_id: str) -> list[str]:
    """Read frontmatter ``related`` tolerantly (see ``_lenient_tags()``).

    A well-formed id that doesn't currently resolve to a file is kept here
    (dangling references are a normal, expected state -- see
    ``MarkdownMemoryStore.related()``); only malformed elements are dropped.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        print(
            f"warning: {memory_id}: ignoring invalid 'related' value (expected a list): {raw!r}",
            file=sys.stderr,
        )
        return []

    normalized: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            print(
                f"warning: {memory_id}: ignoring invalid related element: {item!r}",
                file=sys.stderr,
            )
            continue
        try:
            validate_memory_id(item)
        except StorePathError:
            print(
                f"warning: {memory_id}: ignoring invalid related element: {item!r}",
                file=sys.stderr,
            )
            continue
        if item == memory_id:
            continue
        normalized.add(item)
    return sorted(normalized)


def humanize_key(key: str) -> str:
    """Convert a memory key like ``preferred_editor`` into a title, e.g. ``Preferred Editor``."""
    words = [word for word in _KEY_WORD_SPLIT.split(key.strip()) if word]
    if not words:
        return key
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _entity_prefixed_slug(entity_type: str, entity_id: str, key: str) -> str:
    """Derive the filename base (without directory or ``.md``) for a logical key.

    - The typical case (``entity_type="user"``, ``entity_id="default"``) omits
      the entity from the slug, keeping filenames short.
    - Any other entity is prefixed as ``<entity_slug>-<key_slug>``.
    """
    key_slug = slugify(key)
    if entity_type == "user" and entity_id == "default":
        return key_slug
    return f"{slugify(f'{entity_type}-{entity_id}')}-{key_slug}"


def canonical_memory_id(
    entity_type: str,
    entity_id: str,
    key: str,
    scope: str,
    project_id: str | None,
) -> str:
    """Deterministically derive a human-readable candidate id for a logical memory key.

    The id doubles as the file's path relative to ``memory/`` (without the
    ``.md`` extension): scope-specific directory prefixes are applied here
    (see the module docstring), and ``_entity_prefixed_slug`` derives the
    filename itself.

    This does not resolve filename collisions against an existing Vault; see
    ``MarkdownMemoryStore._resolve_free_slug`` for that.
    """
    base = _entity_prefixed_slug(entity_type, entity_id, key)

    if scope == "global":
        return f"global/{base}"
    if scope == "project" and project_id:
        return f"projects/{slugify(project_id)}/{base}"
    if scope == "client":
        return f"clients/{slugify(entity_id)}/{base}"
    if scope == "temporary":
        return f"temporary/{base}"
    return base


def _scope_info_from_id(memory_id: str) -> tuple[str, str | None, str | None]:
    """Derive ``(scope, project_id, entity_id)`` from a memory id (its path).

    The directory layout is now the sole source of truth for scope/project/
    (client) entity, since frontmatter no longer duplicates it (see the
    module docstring / ``canonical_memory_id``).
    """
    parts = memory_id.split("/")
    if parts[0] == "global":
        return "global", None, None
    if parts[0] == "projects" and len(parts) >= 2:
        return "project", parts[1], None
    if parts[0] == "clients" and len(parts) >= 2:
        return "client", None, parts[1]
    if parts[0] == "temporary":
        return "temporary", None, None
    return "global", None, None


def format_history_date(iso_timestamp: str) -> str:
    """Return the ``YYYY-MM-DD`` date portion of an ISO-8601 timestamp for history lines."""
    return iso_timestamp[:10]


def _updated_sort_key(record: dict[str, Any]) -> datetime:
    """Return a UTC-normalized ``datetime`` for sorting records by ``updated``.

    Comparing ``updated`` as a raw string (as ``search()``/``get_context()``/
    ``_write_index()`` still do -- out of scope for this fix, see
    verification.md) breaks whenever two records use different UTC offsets
    for the same instant, since lexicographic order does not track offset
    (e.g. ``"...T10:00:00+09:00"`` sorts as "newer" than
    ``"...T02:00:00+00:00"`` even though the latter is a later instant).
    This is used by ``related()`` and is written as a reusable, named
    helper (rather than inlined) so the same fix can be applied to the
    other three call sites later without re-deriving it.

    A missing or unparseable value sorts as the oldest possible instant --
    deterministic, and pushes it to the end of a descending sort without
    raising (``sort()`` would otherwise raise ``TypeError`` comparing
    ``None``/an unparseable string against a real ``datetime``).
    """
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    value = record.get("updated")
    if not value:
        return oldest
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return oldest
    # Legacy frontmatter stores bare dates (e.g. "2026-05-06"), which
    # fromisoformat() parses as a naive datetime at midnight; treat that as
    # UTC rather than guessing at an unknown original offset (matches
    # memory.py's parse_timestamp()).
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_line(text: str) -> str:
    """Return the first non-blank line of ``text``, stripped (for compact history lines)."""
    stripped = text.strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def render_history_line(old_summary: str, new_summary: str, changed_date: str) -> str:
    """Render one ``## 変更履歴`` line recording a content change from ``old_summary`` to
    ``new_summary`` on ``changed_date`` (``YYYY-MM-DD``)."""
    return f"{changed_date}: {_first_line(old_summary)} → {_first_line(new_summary)} に変更"


def _parse_markdown(text: str) -> tuple[dict[str, Any], str]:
    """Split a frontmatter Markdown document into (frontmatter, body).

    Frontmatter values may themselves contain a literal ``---`` line (e.g. a
    markdown horizontal rule inside a stored report excerpt), so a naive
    substring split is not safe. ``yaml.safe_dump`` is called with an
    effectively unlimited ``width`` (see ``_render_markdown``) so that no
    scalar is ever wrapped onto a physical line of its own; this makes a
    simple "first two lines that are exactly ---" scan reliable in practice.
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    end_index = None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            end_index = index
            break

    if end_index is None:
        return {}, text

    frontmatter_text = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    return frontmatter, body.lstrip("\n").rstrip("\n")


_LINE_ENDING = re.compile(r"\r\n|\r|\n")


def _split_frontmatter_and_raw_body(text: str) -> tuple[dict[str, Any], str] | None:
    """Like ``_parse_markdown()``, but the body is returned exactly as it
    appears in ``text`` -- no leading/trailing blank-line normalization, and
    no line-ending normalization (CRLF/CR survive untouched).

    Returns ``None`` if no frontmatter block (an opening ``"---"`` line and a
    matching closing ``"---"`` line) can be found -- e.g. a BOM before the
    opening delimiter (``"\\ufeff---"`` does not start with ``"---"``), a
    plain Markdown file with no frontmatter at all, or a truncated file with
    an opening delimiter but no closing one. Unlike ``_parse_markdown()``
    (whose callers treat "no frontmatter" as a legitimate, if unusual, state
    for a freshly-read file), a caller here is always about to *edit* an
    existing memory's frontmatter -- so a failed detection must be
    distinguishable from "frontmatter is legitimately empty" (which doesn't
    happen in practice; ``write()`` always emits ``type``/``created``/
    ``updated``) so ``update_metadata()`` can refuse instead of silently
    starting a new frontmatter block ahead of what would then be
    misinterpreted as body text (see its call site).

    ``update_metadata()`` uses this instead of ``_parse_markdown()`` because
    it must reproduce the body byte-for-byte (see the module's tags/related
    design notes and ``update_metadata()``'s docstring). Two things
    ``_parse_markdown()`` does that would break that guarantee:

    - It strips leading/trailing blank lines for the read path's own record
      shape (title/summary/history parsing).
    - Splitting on a bare ``"\\n"`` (as ``_parse_markdown()`` does) turns a
      CRLF-terminated closing delimiter line into ``"---\\r"``, which never
      equals ``"---"`` -- so a CRLF file's frontmatter boundary would go
      undetected entirely. Scanning with ``_LINE_ENDING`` (CRLF/CR/LF) finds
      the boundary correctly regardless of the file's line-ending style,
      and slicing (rather than split-then-rejoin) leaves every original
      character after that boundary -- including any CRLF in the body --
      untouched. ``update_metadata()`` also must not rely on ``Path.read_text()``
      to obtain ``text`` here, since its universal-newlines translation
      would already have turned "\\r\\n" into "\\n" before this function
      ever sees it (see ``update_metadata()``'s use of ``read_bytes()``).
    """
    if not text.startswith("---"):
        return None

    newline_matches = list(_LINE_ENDING.finditer(text))
    if not newline_matches:
        return None

    line_start = 0
    frontmatter_lines: list[str] = []
    for line_index, match in enumerate(newline_matches):
        line = text[line_start : match.start()]
        line_start = match.end()
        if line_index == 0:
            # The opening "---" line itself; not part of the frontmatter body.
            continue
        if line == "---":
            frontmatter_text = "\n".join(frontmatter_lines)
            raw_body = text[line_start:]
            frontmatter = yaml.safe_load(frontmatter_text) or {}
            return frontmatter, raw_body
        frontmatter_lines.append(line)

    return None


def _render_markdown(frontmatter: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
    )
    return f"---\n{yaml_text}---\n\n{body}\n"


def _split_heading(body: str) -> tuple[str, str]:
    """Split a leading ``# Title`` line (and the blank line after it) off ``body``.

    Returns ``(title, remaining_body)``. This is the inverse of the heading
    that ``MarkdownMemoryStore.write`` adds to the file body, so that
    ``record["title"]``/``record["summary"]`` round-trip.
    """
    lines = body.split("\n")
    if not lines or not lines[0].startswith("# "):
        return "", body

    title = lines[0][2:].strip()
    remaining = lines[1:]
    while remaining and remaining[0] == "":
        remaining.pop(0)
    return title, "\n".join(remaining)


def _split_history(body: str) -> tuple[str, list[str]]:
    """Split ``body`` (with the H1 heading already stripped) into (summary, history_lines).

    The inverse of the ``## 変更履歴`` section that ``MarkdownMemoryStore.write``
    appends after the summary.
    """
    lines = body.split("\n")
    if HISTORY_HEADING not in lines:
        return body, []

    heading_index = lines.index(HISTORY_HEADING)
    summary_lines = lines[:heading_index]
    while summary_lines and summary_lines[-1] == "":
        summary_lines.pop()

    history_lines = [line[2:] for line in lines[heading_index + 1 :] if line.startswith("- ")]
    return "\n".join(summary_lines), history_lines


class MarkdownMemoryStore:
    """File-based store for the stable ``memories`` layer."""

    def __init__(self, vault_dir: Path) -> None:
        self.vault_dir = Path(vault_dir).resolve()
        self.memory_dir = self.vault_dir / "memory"
        checked_path(self.vault_dir, self.memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def transaction(self, timeout: float = 30.0):
        return store_lock(self.vault_dir, timeout)

    def assert_writable(self) -> None:
        """Public entry point for callers outside this module (e.g. the
        one-shot SQLite migration) that must fail fast, before starting a
        multi-step write, without reaching into a private method."""
        self._assert_writable()

    def _assert_writable(self) -> None:
        checked_path(self.vault_dir, self.memory_dir)
        for path in self.memory_dir.rglob("*"):
            checked_path(self.vault_dir, path)
        if self._warn_sync_conflicts():
            raise StorePathError("sync-conflict files must be resolved before modifying the Vault")

    def _path_for_id(self, memory_id: str) -> Path:
        validate_memory_id(memory_id)
        return checked_path(self.vault_dir, self.memory_dir / f"{memory_id}.md")

    def _is_archived(self, path: Path) -> bool:
        return path.relative_to(self.memory_dir).parts[0] == ARCHIVE_DIRNAME

    def _warn_sync_conflicts(self) -> list[Path]:
        conflicts = [
            path
            for path in self.memory_dir.rglob("*.md")
            if SYNC_CONFLICT_PATTERN.search(path.name)
        ]
        if conflicts:
            names = ", ".join(str(path.relative_to(self.memory_dir)) for path in conflicts)
            print(
                f"warning: sync-conflict files detected in {self.memory_dir}: {names}",
                file=sys.stderr,
            )
        return conflicts

    def _existing_ids(self) -> set[str]:
        """Ids currently occupying a canonical slug (archived memories don't count --
        their slug is free to be reclaimed by a new memory, see ``forget()``)."""
        return {
            path.relative_to(self.memory_dir).with_suffix("").as_posix()
            for path in self.memory_dir.rglob("*.md")
            if path.name != INDEX_FILENAME
            and not SYNC_CONFLICT_PATTERN.search(path.name)
            and not self._is_archived(path)
        }

    def _warn_dangling_related(self, memory_id: str, related_ids: list[str]) -> list[str]:
        """Return the subset of ``related_ids`` that aren't currently an active
        memory, warning about each on stderr.

        Called from ``upsert_from_observation()``/``update_metadata()`` right
        after a caller-supplied ``related`` list is normalized. Existence is
        accepted with a warning here, never rejected (see plan.md design
        decision 2): ``forget()`` legitimately produces dangling references
        during normal operation, and ``related()`` already reports them
        separately via its own ``dangling`` field at read time. The warning
        at write time exists because stderr (and thus that read-time signal)
        isn't visible to an MCP-connected LLM -- callers surface this
        return value as ``dangling_related`` in their response instead (see
        ``memory.py``'s ``run_write_memory()``/``run_update_metadata()``).
        """
        if not related_ids:
            return []
        active_ids = self._existing_ids()
        dangling = [related_id for related_id in related_ids if related_id not in active_ids]
        for related_id in dangling:
            print(
                f"warning: {memory_id}: related id does not currently exist "
                f"(accepted as a dangling reference): {related_id}",
                file=sys.stderr,
            )
        return dangling

    def _resolve_free_slug(self, candidate: str) -> str:
        """Return ``candidate``, or ``candidate-2``/``candidate-3``/... if taken.

        Collisions are expected to be rare in practice (see module docstring);
        this is a fallback safety net, not the primary uniqueness mechanism.
        """
        existing_ids = self._existing_ids()
        if candidate not in existing_ids:
            return candidate

        suffix = 2
        while f"{candidate}-{suffix}" in existing_ids:
            suffix += 1
        return f"{candidate}-{suffix}"

    @staticmethod
    def _write_text_atomic(path: Path, text: str) -> None:
        tmp_path = path.with_name(f"{path.stem}.tmp-{uuid.uuid4().hex}{path.suffix}")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
        """Like ``_write_text_atomic()``, but bypasses text-mode newline
        translation entirely -- used by ``update_metadata()`` so a body's
        original line endings (including CRLF) are never at the mercy of a
        platform-dependent no-op (see its call site's comment)."""
        tmp_path = path.with_name(f"{path.stem}.tmp-{uuid.uuid4().hex}{path.suffix}")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)

    @locked
    def write(self, record: dict[str, Any]) -> Path:
        """Atomically write a memory record to its canonical file, then refresh the index."""
        self._assert_writable()
        path = self._path_for_id(record["id"])
        frontmatter: dict[str, Any] = {
            "type": record["type"],
            "created": record["created"],
            "updated": record["updated"],
        }
        tags = record.get("tags") or []
        related = record.get("related") or []
        if tags:
            frontmatter["tags"] = tags
        if related:
            frontmatter["related"] = related
        body_lines = [f"# {record['title']}", "", record.get("summary", "")]
        history = record.get("history") or []
        if history:
            body_lines += ["", HISTORY_HEADING, ""]
            body_lines += [f"- {line}" for line in history]
        body = "\n".join(body_lines)
        text = _render_markdown(frontmatter, body)

        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_text_atomic(path, text)
        self._write_index()
        return path

    @locked
    def read(self, memory_id: str) -> dict[str, Any] | None:
        path = self._path_for_id(memory_id)
        if not path.exists():
            return None
        return self._read_path(path)

    def _read_path(self, path: Path) -> dict[str, Any]:
        checked_path(self.vault_dir, path)
        frontmatter, body = _parse_markdown(path.read_text(encoding="utf-8"))
        title, remaining = _split_heading(body)
        summary, history = _split_history(remaining)
        memory_id = path.relative_to(self.memory_dir).with_suffix("").as_posix()
        scope, project_id, entity_id = _scope_info_from_id(memory_id)
        created = frontmatter.get("created")
        updated = frontmatter.get("updated")
        return {
            "id": memory_id,
            "type": frontmatter.get("type"),
            # PyYAML parses unquoted ISO dates as datetime.date. Normalize
            # legacy hand-edited files to the string form used by sorting and
            # the CLI's JSON output.
            "created": str(created) if created is not None else None,
            "updated": str(updated) if updated is not None else None,
            "title": title,
            "summary": summary,
            "history": history,
            "scope": scope,
            "project_id": project_id,
            "entity_id": entity_id,
            "tags": _lenient_tags(frontmatter.get("tags"), memory_id),
            "related": _lenient_related(frontmatter.get("related"), memory_id),
        }

    @locked
    def iter_all(self) -> list[dict[str, Any]]:
        self._warn_sync_conflicts()
        records = []
        for path in sorted(self.memory_dir.rglob("*.md")):
            if path.name == INDEX_FILENAME:
                continue
            if SYNC_CONFLICT_PATTERN.search(path.name):
                continue
            if self._is_archived(path):
                continue
            records.append(self._read_path(path))
        return records

    @locked
    def migrate_legacy_root_memories(self) -> list[str]:
        """Move pre-layout global memory files into ``memory/global/``.

        The migration is explicit so normal reads never mutate a synced Vault.
        It refuses to overwrite a destination: resolve any duplicate manually
        before retrying, then regenerates the derived index after a successful
        move.
        """
        self._assert_writable()
        legacy_paths = sorted(
            path
            for path in self.memory_dir.glob("*.md")
            if path.name != INDEX_FILENAME and not SYNC_CONFLICT_PATTERN.search(path.name)
        )
        destinations = [self.memory_dir / "global" / path.name for path in legacy_paths]
        collisions = [destination for destination in destinations if destination.exists()]
        if collisions:
            names = ", ".join(str(path.relative_to(self.memory_dir)) for path in collisions)
            raise FileExistsError(
                f"legacy layout migration would overwrite existing files: {names}"
            )

        for source, destination in zip(legacy_paths, destinations, strict=True):
            checked_path(self.vault_dir, source)
            checked_path(self.vault_dir, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)

        if legacy_paths:
            self._write_index()
        return [
            path.relative_to(self.memory_dir).with_suffix("").as_posix() for path in destinations
        ]

    def _find_existing(
        self,
        entity_type: str,
        entity_id: str,
        key: str,
        scope: str,
        project_id: str | None,
    ) -> dict[str, Any] | None:
        """Locate the current file for a logical key, if any.

        The canonical path (deterministic from the logical key, see
        ``canonical_memory_id``) is checked directly rather than scanning the
        whole Vault -- frontmatter no longer stores the logical key's parts
        to scan-match against. The file's H1 heading (``humanize_key(key)``)
        is compared as a cheap sanity check so that an unrelated file that
        happens to occupy the same candidate slug (the rare collision case
        ``_resolve_free_slug`` guards against at creation time) is not
        mistaken for this logical key and silently overwritten.
        """
        candidate_id = canonical_memory_id(entity_type, entity_id, key, scope, project_id)
        record = self.read(candidate_id)
        if record is None or record["title"] != humanize_key(key):
            return None
        return record

    @locked
    def upsert_from_observation(
        self,
        *,
        type: str,
        entity_type: str,
        entity_id: str,
        key: str,
        scope: str,
        project_id: str | None,
        summary: str,
        tags: list[str] | None = None,
        related: list[str] | None = None,
    ) -> dict[str, Any]:
        self._assert_writable()
        if scope == "project" and not project_id:
            raise ValueError(
                f"scope='project' requires a project_id (key={key!r}); refusing to "
                "silently write a global-scope file instead"
            )

        existing = self._find_existing(entity_type, entity_id, key, scope, project_id)
        timestamp = current_timestamp()

        if existing is not None:
            if tags is not None:
                existing["tags"] = normalize_tags(tags)
            if related is not None:
                existing["related"] = normalize_related(related, self_id=existing["id"])
                self._warn_dangling_related(existing["id"], existing["related"])

            if existing["summary"] == summary:
                # Nothing actually changed unless the type was recategorized;
                # bumping `updated` here regardless would misleadingly imply
                # the memory's content was refreshed. Tags/related changes
                # alone never bump `updated` either (see the module's
                # tags/related design notes).
                if existing.get("type") != type:
                    existing["updated"] = timestamp
                existing["type"] = type
                self.write(existing)
                return existing

            # Content changed: fold the old summary into the same file's
            # history section (rather than archiving it to a separate file)
            # and overwrite the body/frontmatter in place with the new
            # current content.
            history_line = render_history_line(
                existing["summary"], summary, format_history_date(timestamp)
            )
            existing["history"] = [*existing.get("history", []), history_line]
            existing["type"] = type
            existing["summary"] = summary
            existing["updated"] = timestamp
            self.write(existing)
            return existing

        candidate_slug = self._resolve_free_slug(
            canonical_memory_id(entity_type, entity_id, key, scope, project_id)
        )
        scope_derived, project_id_derived, entity_id_derived = _scope_info_from_id(candidate_slug)
        final_related = (
            normalize_related(related, self_id=candidate_slug) if related is not None else []
        )
        if related is not None:
            self._warn_dangling_related(candidate_slug, final_related)
        record: dict[str, Any] = {
            "id": candidate_slug,
            "type": type,
            "created": timestamp,
            "updated": timestamp,
            "title": humanize_key(key),
            "summary": summary,
            "history": [],
            "scope": scope_derived,
            "project_id": project_id_derived,
            "entity_id": entity_id_derived,
            "tags": normalize_tags(tags) if tags is not None else [],
            "related": final_related,
        }
        self.write(record)
        return record

    @staticmethod
    def _resolve_free_archive_path(archive_path: Path) -> Path:
        """Return ``archive_path``, or a non-colliding sibling if it is already taken.

        A forgotten slug is free to be reclaimed by a new memory (see
        ``_existing_ids()``), so the same archive destination can be written
        to more than once over the life of a Vault. ``os.replace`` would
        silently overwrite an existing archive file in that case, destroying
        the earlier archived version -- so every archive write must land on
        a fresh path instead of ever overwriting one.
        """
        if not archive_path.exists():
            return archive_path

        # Local time, to match `current_timestamp()`'s frontmatter `created`/`updated`
        # dates -- mixing UTC here and local time there would make an
        # archive's timestamp suffix look inconsistent with its own frontmatter.
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = archive_path.with_name(f"{archive_path.stem}-{timestamp}{archive_path.suffix}")
        suffix = 2
        while candidate.exists():
            candidate = archive_path.with_name(
                f"{archive_path.stem}-{timestamp}-{suffix}{archive_path.suffix}"
            )
            suffix += 1
        return candidate

    @locked
    def forget(self, memory_id: str) -> int:
        """Move a memory's file to ``memory/archive/<same relative path>``.

        This replaces the old ``status: deleted`` frontmatter flag: it is a
        move (``os.replace``), never a deletion, and archived files are
        excluded from ``iter_all()``/``_index.md`` (see module docstring).

        The reclaimed-slug design (see ``_existing_ids()``) means the same
        logical key can be forgotten more than once over time, each time
        producing a file at the same canonical archive path. To avoid
        ``os.replace`` silently overwriting (and losing) an earlier archived
        version, a colliding destination is renamed via
        ``_resolve_free_archive_path`` before the move.
        """
        self._assert_writable()
        path = self._path_for_id(memory_id)
        if not path.exists():
            return 0
        relative = path.relative_to(self.memory_dir)
        archive_path = self._resolve_free_archive_path(self.memory_dir / ARCHIVE_DIRNAME / relative)
        checked_path(self.vault_dir, archive_path)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, archive_path)
        self._write_index()
        return 1

    @locked
    def delete(self, memory_id: str) -> bool:
        self._assert_writable()
        path = self._path_for_id(memory_id)
        if not path.exists():
            return False
        path.unlink()
        self._write_index()
        return True

    @locked
    def search(
        self,
        *,
        query: str | None = None,
        entity_id: str | None = None,
        type: str | None = None,
        scope: str | None = None,
        project_id: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        results = self.iter_all()

        if entity_id:
            results = [r for r in results if r.get("entity_id") == entity_id]
        if type:
            results = [r for r in results if r["type"] == type]
        if scope:
            results = [r for r in results if r["scope"] == scope]
        if project_id:
            results = [r for r in results if r.get("project_id") in (project_id, None)]
        if tags:
            # AND filter: a memory must carry every requested tag (see the
            # module's tags/related design notes -- OR-style "broadly
            # similar" retrieval is `related()`'s job, not search()'s).
            # Callers are expected to normalize tags before calling (see
            # memory.py's run_search()); this compares stored tags as-is.
            required = set(tags)
            results = [r for r in results if required <= set(r.get("tags", []))]
        if query:
            lowered = query.lower()
            results = [
                r
                for r in results
                if lowered in r["title"].lower() or lowered in r["summary"].lower()
            ]

        results.sort(key=lambda r: r["updated"], reverse=True)
        return results

    @locked
    def related(self, memory_id: str, limit: int = 10) -> dict[str, Any]:
        """Rank other memories related to ``memory_id`` by explicit links and shared tags.

        Score: 3.0 per explicit link (counted per direction, so a mutual
        link scores 6.0) + 1.0 per shared tag. Memories that score 0 are
        omitted. Ties break by ``updated`` descending, then ``id`` ascending
        for determinism. Ids listed in ``memory_id``'s own ``related`` that
        no longer resolve to a file are reported separately as ``dangling``
        rather than silently dropped -- `forget()` legitimately produces
        these, and hiding them would make cleanup harder to reason about.
        """
        if limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        record = self.read(memory_id)
        if record is None:
            raise StorePathError(f"memory not found: {memory_id}")

        records = self.iter_all()
        by_id = {r["id"]: r for r in records}
        self_tags = set(record.get("tags", []))
        self_related = set(record.get("related", []))

        hits: list[dict[str, Any]] = []
        for other_id, other in by_id.items():
            if other_id == memory_id:
                continue
            is_outgoing = other_id in self_related
            is_incoming = memory_id in other.get("related", [])
            shared_tags = sorted(self_tags & set(other.get("tags", [])))

            score = (3.0 if is_outgoing else 0.0) + (3.0 if is_incoming else 0.0)
            score += 1.0 * len(shared_tags)
            if score <= 0:
                continue

            if is_outgoing and is_incoming:
                link = "mutual"
            elif is_outgoing:
                link = "outgoing"
            elif is_incoming:
                link = "incoming"
            else:
                link = "none"

            hits.append({**other, "score": score, "matched_tags": shared_tags, "link": link})

        # Stable multi-key sort: apply least-significant key first. `updated`
        # is compared as a UTC-normalized instant (_updated_sort_key()), not
        # as a raw string -- see its docstring for why (verification.md
        # issue 2: a naive string compare puts records from different UTC
        # offsets in the wrong order).
        hits.sort(key=lambda h: h["id"])
        hits.sort(key=_updated_sort_key, reverse=True)
        hits.sort(key=lambda h: h["score"], reverse=True)

        dangling = sorted(rel_id for rel_id in self_related if rel_id not in by_id)
        return {"hits": hits[:limit], "dangling": dangling}

    @locked
    def list_tags(self) -> list[dict[str, Any]]:
        """Return ``{tag, count}`` for every tag in use, ranked by count
        descending then tag name ascending (ties)."""
        counts: dict[str, int] = {}
        for record in self.iter_all():
            for tag in record.get("tags", []):
                counts[tag] = counts.get(tag, 0) + 1
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    @locked
    def update_metadata(
        self,
        memory_id: str,
        *,
        tags: list[str] | None = None,
        related: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace only ``tags``/``related`` in an existing memory's frontmatter.

        Unlike ``write()``, this never reconstructs the body from
        title/summary/history: it re-parses the file's own current text and
        only swaps the requested frontmatter field(s), so the body (title,
        change history, trailing newline) and any untouched frontmatter
        value (including ``type``/``created``/``updated``, unknown keys, and
        an unrequested field's value even if it is itself malformed) survive
        byte-for-byte (only the YAML layout of the touched keys may be
        re-rendered). No observation/session/event is created and
        ``summarize_memory()`` is never invoked -- this is a pure metadata
        edit, not a content change (see the module's tags/related design
        notes for the full rationale).
        """
        if tags is None and related is None:
            raise ValueError("update_metadata requires tags or related to be provided")
        self._assert_writable()
        path = self._path_for_id(memory_id)
        if not path.exists():
            raise ValueError(f"memory not found: {memory_id}")

        # read_bytes() + manual decode, not Path.read_text(): text-mode reads
        # apply universal-newlines translation ("\r\n"/"\r" -> "\n") before
        # any string processing ever runs, which would silently turn a
        # CRLF-edited body into LF and violate the byte-for-byte guarantee
        # above (see _split_frontmatter_and_raw_body()'s docstring).
        original_bytes = path.read_bytes()
        original_text = original_bytes.decode("utf-8")
        split = _split_frontmatter_and_raw_body(original_text)
        if split is None:
            # Never fabricate a fresh frontmatter block ahead of what would
            # then be misread as body text (e.g. a BOM before the opening
            # delimiter, no frontmatter at all, or a truncated file with no
            # closing delimiter) -- see _split_frontmatter_and_raw_body()'s
            # docstring. Refuse instead of silently corrupting the file.
            raise ValueError(
                f"cannot update metadata for {memory_id}: no frontmatter block detected "
                "(possible BOM or missing/unterminated '---' delimiter)"
            )
        frontmatter, raw_body = split

        new_frontmatter = dict(frontmatter)
        dangling_related: list[str] = []
        if tags is not None:
            normalized_tags = normalize_tags(tags)
            if normalized_tags:
                new_frontmatter["tags"] = normalized_tags
            else:
                new_frontmatter.pop("tags", None)
        if related is not None:
            normalized_related = normalize_related(related, self_id=memory_id)
            dangling_related = self._warn_dangling_related(memory_id, normalized_related)
            if normalized_related:
                new_frontmatter["related"] = normalized_related
            else:
                new_frontmatter.pop("related", None)

        ordered_frontmatter = {
            key: new_frontmatter[key] for key in _FRONTMATTER_CORE_ORDER if key in new_frontmatter
        }
        for key, value in new_frontmatter.items():
            if key not in _FRONTMATTER_CORE_ORDER:
                ordered_frontmatter[key] = value

        # Deliberately not _render_markdown(): that helper always inserts a
        # blank line + trailing newline meant for the store's own normalized
        # body shape (see write()). Re-rendering only the frontmatter and
        # reusing the body's exact raw text (from
        # _split_frontmatter_and_raw_body()) is what keeps a hand-edited
        # body's whitespace -- including multiple trailing blank lines --
        # byte-for-byte (see the module's tags/related design notes).
        yaml_text = yaml.safe_dump(
            ordered_frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1_000_000,
        )
        new_text = f"---\n{yaml_text}---\n{raw_body}"
        # Encode and compare/write as bytes, not str: on this store's own
        # target platforms text-mode writes only translate bare "\n" (a
        # no-op on POSIX, since os.linesep == "\n"), but relying on that
        # platform-specific no-op would be fragile -- writing bytes directly
        # is what actually guarantees the body's original line endings
        # (including CRLF) survive regardless of platform.
        new_bytes = new_text.encode("utf-8")
        if new_bytes != original_bytes:
            self._write_bytes_atomic(path, new_bytes)
            self._write_index()

        result = self._read_path(path)
        # An ephemeral, non-persisted field reporting this call's own
        # existence check (see _warn_dangling_related()) -- empty when
        # `related` wasn't part of this call, not a summary of the file's
        # full related list. memory.py's run_update_metadata() surfaces
        # this as a top-level `dangling_related` in its JSON response,
        # since stderr isn't visible to an MCP-connected LLM.
        result["dangling_related"] = dangling_related
        return result

    @locked
    def get_context(self, *, project_id: str | None) -> list[dict[str, Any]]:
        """Return memories relevant to the current session: every global-scope
        memory, plus project-scoped memories for ``project_id``.

        Global memories are no longer filtered by ``entity_id`` -- this store
        serves a single local default user (see ``llm-shared-memory-design.md``),
        and frontmatter no longer records an entity to filter on.
        """
        results = self.iter_all()
        matched = [
            record
            for record in results
            if record["scope"] == "global"
            or (record["scope"] == "project" and record.get("project_id") == project_id)
        ]
        matched.sort(key=lambda r: r["updated"], reverse=True)
        return matched

    @locked
    def rebuild_index(self) -> None:
        """Recreate the derived index after synchronization or a partial failure."""
        self._write_index()

    @locked
    def _write_index(self) -> None:
        """Regenerate ``_index.md``: a human-browsable listing of (non-archived) memories.

        This is a write-only derived artifact (not read back by this store);
        it exists purely so the Vault directory is browsable in Obsidian
        without running a search command. Sections mirror the on-disk
        directory grouping by ``scope`` (see the module docstring).
        """
        self._assert_writable()
        checked_path(self.vault_dir, self.memory_dir / INDEX_FILENAME)
        records = sorted(self.iter_all(), key=lambda r: r["title"])
        global_records = [r for r in records if r["scope"] == "global"]
        project_ids = sorted(
            {r["project_id"] for r in records if r["scope"] == "project" and r.get("project_id")}
        )
        client_ids = sorted(
            {r["entity_id"] for r in records if r["scope"] == "client" and r.get("entity_id")}
        )
        temporary_records = [r for r in records if r["scope"] == "temporary"]

        lines = [
            "# Memory Index",
            "",
            "## Global",
            *(self._index_lines(global_records) or ["(none)"]),
        ]

        for project_id in project_ids:
            project_records = [
                r for r in records if r["scope"] == "project" and r.get("project_id") == project_id
            ]
            lines.append("")
            lines.append(f"## Project: {project_id}")
            lines.extend(self._index_lines(project_records))

        for client_id in client_ids:
            client_records = [
                r for r in records if r["scope"] == "client" and r.get("entity_id") == client_id
            ]
            lines.append("")
            lines.append(f"## Client: {client_id}")
            lines.extend(self._index_lines(client_records))

        if temporary_records:
            lines.append("")
            lines.append("## Temporary")
            lines.extend(self._index_lines(temporary_records))

        text = "\n".join(lines).rstrip("\n") + "\n"
        self._write_text_atomic(self.memory_dir / INDEX_FILENAME, text)

    @staticmethod
    def _index_lines(records: list[dict[str, Any]]) -> list[str]:
        lines = []
        for record in records:
            excerpt = record["summary"].splitlines()[0].strip() if record.get("summary") else ""
            line = f"- [{record['title']}]({record['id']}.md) — {excerpt}"
            tags = record.get("tags") or []
            if tags:
                # Inline code, not `#tag`: `_index.md` is a generated listing
                # of every memory, so a literal `#tag` here would double-count
                # every tag in Obsidian's tag pane and make `_index.md` itself
                # show up in every tag search (see the module's tags/related
                # design notes). `related` is deliberately not shown here --
                # link browsing is Obsidian's property view / the `related`
                # tool's job, not the index's.
                line += f" `tags: {', '.join(tags)}`"
            lines.append(line)
        return lines
