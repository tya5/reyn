"""Tier 2: read_tool_result tool — companion to #385 PoC preview-driven
tool returns.

Pins the contract that LLM-callable tool ``read_tool_result(path=...)``:

1. Returns ``status="ok"`` + ``content`` when the path is valid and the
   file exists inside ``.reyn/tool-results/``.
2. Returns ``status="not_found"`` when the file was deleted (= user
   manually cleaned up under ``.reyn/tool-results/``).
3. Returns ``status="error"`` with a PermissionError-derived message
   when the path tries to escape the workspace boundary (= path
   traversal / path-ref injection).
4. Truncates at ``max_bytes`` with a clear ``truncated: True`` signal
   so the LLM can decide to re-call with a higher cap.
5. Surfaces a structured error (= ``status="error"``) when the session
   has no ``MediaStore`` configured rather than crashing — keeps the
   PoC degrade-safe for sessions outside the multimodal path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.tools.read_tool_result import _handle
from reyn.tools.types import (
    PhaseCallerState,
    RouterCallerState,
    ToolContext,
)
from reyn.workspace.media_store import MediaStore, MediaStoreConfig


class _StubEvents:
    """Minimal stand-in for the events log — the read tool emits none,
    but ToolContext requires the attribute.
    """
    def emit(self, *args, **kwargs) -> None:
        pass

    subscribers: list = []


def _populate_tool_result(
    tmp_path: Path, content: str = "hello\nworld",
) -> tuple[MediaStore, str]:
    """Build a MediaStore, write a tool result, return (store, path-ref-str)."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    block = store.save_tool_result(
        content, mime_type="text/plain",
        chain_id="abc123", tool="web_fetch", seq=1,
    )
    return store, block["path"]


def _ctx_with_media_store(media_store: MediaStore | None) -> ToolContext:
    """Build a minimal router-caller ToolContext whose router_state
    factory hands back an OpContext carrying ``media_store``.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    def _factory() -> OpContext:
        return OpContext(
            workspace=None,
            events=_StubEvents(),
            permission_decl=PermissionDecl(),
            permission_resolver=None,
            skill_name="",
            subscribers=[],
            media_store=media_store,
        )

    return ToolContext(
        events=_StubEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(op_context_factory=_factory),
        phase_state=None,
    )


# ── happy path ─────────────────────────────────────────────────────────


def test_read_tool_result_returns_full_content_when_below_cap(tmp_path):
    """Tier 2: small file under default max_bytes returns full content
    with ``truncated=False``.
    """
    store, path_ref = _populate_tool_result(tmp_path, "hello\nworld\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref}, ctx))

    assert result["status"] == "ok"
    assert result["path"] == path_ref
    assert result["content"] == "hello\nworld\n"
    assert result["truncated"] is False
    assert result["total_bytes"] == len("hello\nworld\n".encode("utf-8"))


def test_read_tool_result_truncates_when_above_max_bytes(tmp_path):
    """Tier 2: content larger than ``max_bytes`` truncates and surfaces
    ``truncated=True`` + ``total_bytes`` so the LLM can re-call with a
    higher cap.
    """
    big = "a" * 5000
    store, path_ref = _populate_tool_result(tmp_path, big)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "max_bytes": 1000}, ctx))

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["max_bytes"] == 1000
    assert result["total_bytes"] == 5000
    assert len(result["content"]) == 1000


# ── error / edge cases ─────────────────────────────────────────────────


def test_read_tool_result_missing_path_arg_returns_error(tmp_path):
    """Tier 2: empty / missing ``path`` argument surfaces a structured
    error without touching the filesystem.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({}, ctx))

    assert result["status"] == "error"
    assert "path is required" in result["error"]


def test_read_tool_result_outside_tool_results_dir_rejected(tmp_path):
    """Tier 2: a path that escapes ``.reyn/tool-results/`` (e.g. via
    ``..``) is rejected with an error rather than read.

    Defends against an adversarial / malformed path-ref smuggling in a
    file outside the workspace media boundary.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    ctx = _ctx_with_media_store(store)

    # A path that escapes via .. — read_tool_result on MediaStore raises
    # PermissionError, the tool handler catches it and surfaces the
    # message under ``error``.
    result = asyncio.run(
        _handle({"path": "../../../etc/passwd"}, ctx),
    )

    assert result["status"] == "error"
    assert "outside" in result["error"]


def test_read_tool_result_missing_file_returns_not_found(tmp_path):
    """Tier 2: a path inside ``tool_results_dir`` whose file no longer
    exists (= user deleted via ``rm``) surfaces ``status=not_found``
    rather than crashing.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    # Create the directory so the path validation succeeds, then point
    # at a file that doesn't exist inside it.
    store.tool_results_dir.mkdir(parents=True, exist_ok=True)
    fake_rel = str(
        (store.tool_results_dir / "deleted-file.txt").relative_to(tmp_path)
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": fake_rel}, ctx))

    assert result["status"] == "not_found"
    assert result["path"] == fake_rel


def test_read_tool_result_without_media_store_degrades_with_error(tmp_path):
    """Tier 2: when the session has no MediaStore (= legacy / non-
    multimodal path), the tool returns a structured error rather than
    crashing — keeps the PoC degrade-safe.
    """
    ctx = _ctx_with_media_store(media_store=None)

    result = asyncio.run(
        _handle({"path": ".reyn/tool-results/anything.txt"}, ctx),
    )

    assert result["status"] == "error"
    assert "MediaStore" in result["error"]


# ── offset / limit line-slice (= PR #409 4-surface symmetry adoption, Q7) ──


def test_read_tool_result_offset_only_skips_leading_lines(tmp_path):
    """Tier 2: ``offset=N`` starts at line N (0-indexed), reads through
    end-of-body. Mirrors ``read_file`` / ``reyn_src_read`` /
    ``read_memory_body`` semantics introduced in PR #409.
    """
    store, path_ref = _populate_tool_result(
        tmp_path, "L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "offset": 2}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "L2\nL3\nL4\n"


def test_read_tool_result_limit_only_takes_first_n(tmp_path):
    """Tier 2: ``limit=N`` without ``offset`` takes the first N lines."""
    store, path_ref = _populate_tool_result(
        tmp_path, "L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "limit": 2}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "L0\nL1\n"


def test_read_tool_result_offset_and_limit_window(tmp_path):
    """Tier 2: combining ``offset`` + ``limit`` returns the
    ``[offset, offset+limit)`` line window.
    """
    store, path_ref = _populate_tool_result(
        tmp_path, "L0\nL1\nL2\nL3\nL4\n",
    )
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle({"path": path_ref, "offset": 1, "limit": 2}, ctx),
    )

    assert result["status"] == "ok"
    assert result["content"] == "L1\nL2\n"


def test_read_tool_result_offset_past_eof_returns_empty(tmp_path):
    """Tier 2: ``offset`` past the body's last line returns empty
    content — never an error. Matches the past-EOF semantic of the
    three sister read tools so the LLM detects out-of-range without a
    structured failure path.
    """
    store, path_ref = _populate_tool_result(tmp_path, "L0\nL1\nL2\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref, "offset": 99}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == ""


def test_read_tool_result_slice_then_max_bytes_compose(tmp_path):
    """Tier 2: ``offset`` / ``limit`` (= line slice) are applied BEFORE
    ``max_bytes`` (= byte cap) — the two axes compose. Verifies the
    slice happens against the full body, then the resulting sliced
    text is byte-capped if it still exceeds ``max_bytes``.

    Scenario: 100 lines of 20-char content (= ~2000 bytes). offset=10,
    limit=50 → sliced ~1000 bytes. max_bytes=300 → final 300 bytes of
    the sliced window, ``truncated=True`` on the sliced size.
    """
    lines = [f"line {i:>3} content" for i in range(100)]  # 16-char each
    body = "\n".join(lines) + "\n"
    store, path_ref = _populate_tool_result(tmp_path, body)
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(
        _handle(
            {"path": path_ref, "offset": 10, "limit": 50, "max_bytes": 300},
            ctx,
        ),
    )

    assert result["status"] == "ok"
    # max_bytes hit, surfaces truncated=True.
    assert result["truncated"] is True
    assert result["max_bytes"] == 300
    # content starts at offset=10 (= "line  10 content"), not the file head.
    assert result["content"].startswith("line  10 content")
    # content is exactly 300 bytes (= max_bytes cap on the sliced window).
    assert len(result["content"].encode("utf-8")) == 300


def test_read_tool_result_no_slice_args_is_unchanged_behaviour(tmp_path):
    """Tier 2: omitting ``offset`` / ``limit`` preserves the prior
    full-body / max_bytes-only behaviour — backwards-compatible.
    """
    store, path_ref = _populate_tool_result(tmp_path, "alpha\nbeta\ngamma\n")
    ctx = _ctx_with_media_store(store)

    result = asyncio.run(_handle({"path": path_ref}, ctx))

    assert result["status"] == "ok"
    assert result["content"] == "alpha\nbeta\ngamma\n"
    assert result["truncated"] is False
