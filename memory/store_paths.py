"""Validate portable record IDs and reject links below a resolved store root.

This is not isolation against a local user replacing paths during an operation.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path


class StorePathError(ValueError):
    """An identifier or stored path violates the storage boundary."""


def validate_component(value: str) -> str:
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise StorePathError("ID must be a nonempty filename component")
    if any(ord(char) < 32 or char in '/\\:*?"<>|' for char in value) or value.endswith((" ", ".")):
        raise StorePathError("ID contains an unsafe filename component")
    if re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.IGNORECASE):
        raise StorePathError("ID uses a reserved device name")
    return value


def validate_memory_id(value: str) -> str:
    if not isinstance(value, str):
        raise StorePathError("memory ID must be a string")
    parts = value.split("/")
    if parts[-1] == "_index" or ".sync-conflict-" in parts[-1]:
        raise StorePathError("index and synchronization conflicts are not active memories")
    for part in parts:
        validate_component(part)
    if any(part == "_index" or "sync-conflict" in part for part in parts):
        raise StorePathError("index and synchronization conflict files are not memory IDs")
    if not (
        (parts[0] in ("global", "temporary") and len(parts) == 2)
        or (parts[0] in ("projects", "clients") and len(parts) == 3)
    ):
        raise StorePathError("memory ID must identify an active memory")
    return value


def checked_path(root: Path, path: Path) -> Path:
    """Check every existing child, including dangling symlinks and reparse points."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StorePathError("path is outside the store") from exc
    current = root
    for component in relative.parts:
        if component in (".", ".."):
            raise StorePathError("path is outside the store")
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise StorePathError(f"links are not allowed inside the store: {current}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise StorePathError("resolved path is outside the store") from exc
    return path
