"""stdio MCP adapter for the shared-memory service."""

from __future__ import annotations

import sys
import threading
from argparse import Namespace
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

import memory
from local_store import LocalPipelineStore, new_id
from markdown_store import MarkdownMemoryStore
from memory_config import MemoryConfigError, resolve_paths


def create_server() -> FastMCP:
    """Build a server with stores resolved once for this process."""
    paths = resolve_paths(require_vault=True)
    print(
        f"shared-memory: vault={paths.vault} local={paths.local_dir} queue={paths.queue_dir}",
        file=sys.stderr,
    )
    markdown = MarkdownMemoryStore(paths.vault)
    local = LocalPipelineStore(paths.local_dir)
    server = FastMCP("shared-memory")
    # Keyed by the *effective* project_id (see write_memory), one lazily
    # created session per project for the life of this process.
    sessions: dict[str | None, str] = {}
    # Guards the check-then-create of an automatic session below. With SDK
    # 1.29.1, FuncMetadata.call_fn_with_arg_validation calls a sync tool
    # function directly on the event loop thread (no thread-pool dispatch),
    # so two write_memory calls can't actually interleave inside this
    # critical section today -- see test_concurrent_writes_for_same_project_
    # create_one_session, which has to reach past call_tool() into the raw
    # function on real OS threads to exercise a race at all. This lock is
    # cheap insurance against a future SDK offloading sync tools to a
    # thread/task pool, at which point the race described above would
    # become real without it.
    sessions_lock = threading.Lock()

    def args(**values: Any) -> Namespace:
        defaults = dict(
            db=None,
            markdown_store=markdown,
            local_store=local,
            project_id=None,
            user_id="default",
            client="mcp",
            session_id=None,
            query=None,
            memory_type=None,
            scope=None,
            entity_id=None,
            entity_type="user",
            key=None,
            summary=None,
            confidence=0.8,
            limit=10,
            include_memories=True,
            include_sessions=True,
            include_events=True,
            role=None,
            kind=None,
            memory_id=None,
            reason=None,
            source="mcp",
            extractor_version="mcp-v1",
            require_session=False,
        )
        defaults.update(values)
        return Namespace(**defaults)

    def call(name: str, **values: Any) -> dict[str, Any]:
        try:
            return getattr(memory, f"run_{name}")(args(**values))
        except (memory.MemoryUsageError, MemoryConfigError, ValueError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    def client_label(ctx: Context | None) -> str:
        """Resolve the connecting client's declared name for session provenance.

        ``ctx.session`` raises outside of a live request (e.g. when a tool is
        invoked in-process by tests without a client connection), so that
        case falls back to the bare "mcp" label rather than propagating the
        error. The MCP spec does not guarantee ``clientInfo`` is populated
        either, so a missing name also falls back to "mcp" (verified against
        SDK 1.29.1's stdio client, which does send it -- see check_mcp.py).
        """
        if ctx is None:
            return "mcp"
        try:
            session = ctx.session
        except ValueError:
            return "mcp"
        client_params = session.client_params
        info = client_params.clientInfo if client_params is not None else None
        return f"mcp:{info.name}" if info is not None and info.name else "mcp"

    @server.tool()
    def get_context(project_id: str | None = None) -> dict[str, Any]:
        """現在の共有コンテキストを取得する。"""
        return call("get_context", project_id=project_id)

    @server.tool()
    def search(
        query: str = "",
        memory_type: Literal["profile", "feedback", "reference"] | None = None,
        scope: Literal["global", "project", "client", "temporary"] | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
    ) -> dict[str, Any]:
        """共有メモリを検索する。"""
        return call(
            "search",
            query=query,
            memory_type=memory_type,
            scope=scope,
            project_id=project_id,
            entity_id=entity_id,
            limit=limit,
        )

    @server.tool()
    def history(
        query: str | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
        memory_type: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
        include_memories: bool = True,
        include_sessions: bool = True,
        include_events: bool = True,
    ) -> dict[str, Any]:
        """記憶・セッション・イベントの履歴を検索する。"""
        return call(
            "history",
            query=query,
            project_id=project_id,
            entity_id=entity_id,
            memory_type=memory_type,
            role=role,
            kind=kind,
            limit=limit,
            include_memories=include_memories,
            include_sessions=include_sessions,
            include_events=include_events,
        )

    @server.tool()
    def write_memory(
        key: str,
        summary: str,
        memory_type: Literal["profile", "feedback", "reference"],
        confidence: Annotated[float, Field(strict=True, ge=0, le=1)] = 0.8,
        scope: Literal["global", "project"] = "global",
        project_id: str | None = None,
        entity_type: str = "user",
        entity_id: str = "default",
        session_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """長期的に有効な記憶を保存する。"""
        # session_id reflects only the caller's input from here on; whether an
        # automatic session gets created below must not change this (see M-3).
        explicit_session = session_id is not None
        # Validate all user input before creating the lazy session.
        candidate = args(
            key=key,
            summary=summary,
            memory_type=memory_type,
            confidence=confidence,
            scope=scope,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            session_id=session_id,
            require_session=explicit_session,
        )
        # The effective project_id (after session inheritance / project-entity
        # fallback) is what an automatic session must be keyed and tagged
        # with, not the raw (possibly None) project_id argument (see M-4).
        effective_project_id = memory.validate_write_memory(candidate)
        if not explicit_session:
            with sessions_lock:
                session_id = sessions.get(effective_project_id)
                if session_id is None:
                    session_id = new_id("sess")
                    local.ensure_session(
                        session_id,
                        client=client_label(ctx),
                        user_id="default",
                        project_id=effective_project_id,
                    )
                    sessions[effective_project_id] = session_id
        return call(
            "write_memory",
            key=key,
            summary=summary,
            memory_type=memory_type,
            confidence=confidence,
            scope=scope,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            session_id=session_id,
            source="mcp_extract" if explicit_session else "mcp",
            require_session=explicit_session,
        )

    @server.tool()
    def forget(memory_id: str, reason: str) -> dict[str, Any]:
        """指定した記憶をアーカイブする。"""
        return call("forget", memory_id=memory_id, reason=reason)

    @server.tool()
    def list_unextracted(
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
    ) -> dict[str, Any]:
        """未処理セッションを一覧する。"""
        return call("list_unextracted", limit=limit)

    @server.tool()
    def mark_extracted(session_id: str) -> dict[str, Any]:
        """指定したセッションを処理済みにする。"""
        return call("mark_extracted", session_id=session_id)

    return server


if __name__ == "__main__":
    create_server().run("stdio")
