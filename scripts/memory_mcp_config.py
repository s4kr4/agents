"""Generate MCP registration; apply only explicitly selected, non-secret files."""

import argparse
import json
import os
import stat
import sys
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any


def server_entry(uv: str, agents: str) -> dict[str, Any]:
    """Keep executable and arguments separate, including paths with spaces."""
    project = str(Path(agents) / "memory")
    return {
        "command": uv,
        "args": [
            "run",
            "--locked",
            "--project",
            project,
            str(Path(project) / "mcp_server.py"),
        ],
    }


def _parse(content: bytes, client: str) -> Any:
    if client not in {"codex", "claude-code", "claude-desktop"}:
        raise ValueError("unknown client")
    try:
        if client == "codex":
            import tomlkit

            return tomlkit.parse(content.decode("utf-8"))
        return json.loads(content or b"{}")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid configuration syntax; file was not changed") from exc


def _render(content: bytes, client: str, entry: dict[str, Any]) -> bytes:
    document = _parse(content, client)
    if not isinstance(document, MutableMapping):
        raise TypeError("configuration must be an object/table")
    servers_key = "mcp_servers" if client == "codex" else "mcpServers"
    if servers_key not in document:
        document[servers_key] = {}
    servers = document[servers_key]
    if not isinstance(servers, MutableMapping):
        raise TypeError("server collection must be an object/table")
    if "shared-memory" not in servers:
        servers["shared-memory"] = {}
    target = servers["shared-memory"]
    if not isinstance(target, MutableMapping):
        raise TypeError("shared-memory must be an object/table")
    if "url" in target or target.get("type", "stdio") != "stdio":
        raise ValueError("existing shared-memory uses a different transport")
    if all(target.get(k) == v for k, v in entry.items()):
        return content
    for field, value in entry.items():
        target[field] = value
    if client == "codex":
        import tomlkit

        return tomlkit.dumps(document).encode("utf-8")
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _snapshot(path: Path) -> tuple[bytes, tuple[int, int, int, int] | None]:
    if path.is_symlink():
        raise ValueError("linked configuration: explicitly select the real file")
    try:
        info = path.stat()
    except FileNotFoundError:
        return b"", None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("configuration is not a regular file")
    return path.read_bytes(), (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_mode)


def _before_replace() -> None:
    """Boundary used by race tests; callers recheck state after staging."""


def _verify(path: Path, client: str, entry: dict[str, Any]) -> None:
    document = _parse(path.read_bytes(), client)
    key = "mcp_servers" if client == "codex" else "mcpServers"
    if any(document[key]["shared-memory"].get(k) != v for k, v in entry.items()):
        raise ValueError("post-write verification failed")


def _stage(path: Path, content: bytes, mode: int) -> Path:
    fd, name = tempfile.mkstemp(prefix=".memory-mcp-", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(staging, mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return staging


def apply_config(
    path: Path, client: str, entry: dict[str, Any], *, non_secret: bool = False
) -> str:
    """Preserve unowned fields and abort if a staged update becomes stale."""
    if path.exists() and not non_secret:
        raise ValueError(
            "existing file requires --non-secret-config; do not use it for files containing credentials"
        )
    if any(
        part == ".env"
        or part.startswith(".env.")
        or part in {"secrets", "id_rsa", "id_ed25519", "credentials.json", "auth.json"}
        for part in path.parts
    ):
        raise ValueError("sensitive configuration path is not supported")
    original, state = _snapshot(path)
    updated = _render(original, client, entry)
    if updated == original:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(state[3]) if state else 0o600
    staged = _stage(path, updated, mode)
    # Backups live outside the repository/sync tree, with owner-only permissions.
    with tempfile.TemporaryDirectory(prefix="memory-mcp-backup-") as backup_dir:
        backup = Path(backup_dir) / "config"
        backup.write_bytes(original)
        backup.chmod(mode & 0o600)
        try:
            _before_replace()
            if _snapshot(path) != (original, state):
                raise ValueError("configuration changed while preparing update")
            os.replace(staged, path)
            written_state = _snapshot(path)
            if written_state[0] != updated:
                raise ValueError(
                    "configuration changed after replace; rollback refused"
                )
            try:
                _verify(path, client, entry)
            except Exception:
                if _snapshot(path) != written_state:
                    raise ValueError(
                        "verification failed and configuration changed; rollback refused"
                    ) from None
                if state is None:
                    path.unlink()
                else:
                    restored = _stage(path, backup.read_bytes(), stat.S_IMODE(state[3]))
                    os.replace(restored, path)
                raise
        finally:
            staged.unlink(missing_ok=True)
    return "updated"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client", choices=["claude-code", "claude-desktop", "codex"], required=True
    )
    parser.add_argument("--uv", required=True)
    parser.add_argument(
        "--agents-path", default=str(Path(__file__).resolve().parents[1])
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--non-secret-config",
        action="store_true",
        help="attest the explicitly selected file contains no credentials",
    )
    args = parser.parse_args()
    if not Path(args.uv).is_absolute() or not Path(args.agents_path).is_absolute():
        parser.error("--uv and --agents-path must be native absolute paths")
    entry = server_entry(args.uv, args.agents_path)
    if not args.apply:
        print(_render(b"", args.client, entry).decode("utf-8"), end="")
        print(
            "permissions: pending client verification; generated command/args only",
            file=sys.stderr,
        )
        return 0
    if args.config is None:
        parser.error("--apply requires an explicit --config path")
    try:
        print(
            apply_config(
                args.config, args.client, entry, non_secret=args.non_secret_config
            )
        )
    except (ValueError, TypeError, OSError) as exc:
        # Avoid parser exceptions exposing configuration source text.
        print(
            "failed: configuration update rejected ("
            + type(exc).__name__
            + "); check syntax, non-secret declaration, links and concurrent edits",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
