"""Resolve and validate all memory storage paths before opening stores."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Mapping

try:
    import tomllib
except ImportError:  # CLI remains usable without a config on Python 3.10.
    tomllib = None  # type: ignore[assignment]


class MemoryConfigError(ValueError):
    """Configuration cannot be used safely; never fall back on this error."""


@dataclass(frozen=True)
class MemoryPaths:
    vault: Path
    local_dir: Path
    queue_dir: Path
    used_fallback: bool


def default_config_path(platform: str, env: Mapping[str, str], home: str) -> PurePath:
    """Calculate OS paths without changing the host's pathlib implementation."""
    if platform == "nt":
        root: PurePath = PureWindowsPath(
            env.get("APPDATA", str(PureWindowsPath(home) / "AppData" / "Roaming"))
        )
        return root / "llm-memory" / "config.toml"
    root = PurePosixPath(env.get("XDG_CONFIG_HOME", str(PurePosixPath(home) / ".config")))
    return root / "llm-memory" / "config.toml"


def _path(value: str | Path, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise MemoryConfigError(f"{label} must be a non-empty absolute path")
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    if re.search(r"\$[A-Za-z_]|\$\{|%[^%]+%", expanded):
        raise MemoryConfigError(f"{label} contains an unresolved environment variable")
    path = Path(expanded)
    if not path.is_absolute():
        raise MemoryConfigError(f"{label} must be an absolute path")
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise MemoryConfigError(f"cannot resolve {label}: {exc}") from exc


def _load_config() -> dict[str, Path]:
    explicit = "LLM_MEMORY_CONFIG" in os.environ
    raw = (
        os.environ["LLM_MEMORY_CONFIG"]
        if explicit
        else str(default_config_path(os.name, os.environ, str(Path.home())))
    )
    path = _path(raw, "LLM_MEMORY_CONFIG")
    try:
        path.stat()
    except FileNotFoundError:
        if explicit:
            raise MemoryConfigError(f"explicit memory config does not exist: {path}") from None
        return {}
    except OSError as exc:
        raise MemoryConfigError(f"cannot access memory config {path}: {exc}") from exc
    if tomllib is None:
        raise MemoryConfigError("memory config requires Python >=3.11; run using uv")
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        # TOML errors may contain input values; avoid echoing file contents.
        raise MemoryConfigError(
            f"cannot read/parse memory config {path} ({type(exc).__name__})"
        ) from exc
    if data.keys() - {"vault", "local_dir", "queue_dir"}:
        raise MemoryConfigError(f"unknown keys in memory config {path}")
    return {key: _path(value, f"config {key}") for key, value in data.items()}


def resolve_paths(
    *,
    vault: str | Path | None = None,
    local_dir: str | Path | None = None,
    queue_dir: str | Path | None = None,
    require_vault: bool = False,
) -> MemoryPaths:
    config = _load_config()
    module_dir = Path(__file__).resolve().parent
    defaults = {
        "vault": module_dir / "vault",
        "local_dir": module_dir / "local",
        "queue_dir": Path.home() / ".cache" / "llm-memory" / "queue",
    }
    overrides = {"vault": vault, "local_dir": local_dir, "queue_dir": queue_dir}
    env_keys = {
        "vault": "LLM_MEMORY_VAULT",
        "local_dir": "LLM_MEMORY_LOCAL_DIR",
        "queue_dir": "LLM_MEMORY_QUEUE_DIR",
    }
    resolved = {}
    fallback = vault is None and "LLM_MEMORY_VAULT" not in os.environ and "vault" not in config
    if require_vault and fallback:
        raise MemoryConfigError(
            "MCP requires an explicit vault in LLM_MEMORY_VAULT or memory config"
        )
    for key, default in defaults.items():
        value = overrides[key]
        if value is None:
            value = os.environ.get(env_keys[key], config.get(key, default))
        path = _path(value, key)
        try:
            # Detect files even in a parent of a not-yet-created destination.
            for parent in (path, *path.parents):
                if parent.exists() and not parent.is_dir():
                    raise MemoryConfigError(f"{key} must be a directory: {path}")
        except OSError as exc:
            raise MemoryConfigError(f"cannot access {key} directory {path}: {exc}") from exc
        resolved[key] = path
    return MemoryPaths(**resolved, used_fallback=fallback)


def resolve_vault_dir(explicit: Path | None = None) -> tuple[Path, bool]:
    paths = resolve_paths(vault=explicit)
    return paths.vault, paths.used_fallback


def resolve_local_dir(explicit: Path | None = None) -> Path:
    return resolve_paths(local_dir=explicit).local_dir


def resolve_queue_dir(explicit: Path | None = None) -> Path:
    return resolve_paths(queue_dir=explicit).queue_dir
