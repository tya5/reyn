"""Tier 2: #2296 — file.read stamps a positive ``_self_bounded`` flag on its self-bounding paths.

file.read self-bounds its content ≤ the inline cap (#1209). Originally (#2296) this flag was
consumed by the now-deleted ``context_builder.offload_control_ir_result`` (retired in #2396 Step 4,
its last caller — the ContextFrame-driven phase path — removed by earlier convergence steps) to
exempt an already-bounded read from a redundant re-offload. The offload-exemption behavior itself
went away with that function; these tests keep pinning the surviving half — that ``file.read``'s
`handle()` still stamps ``_self_bounded`` on every self-bounding path (truncated, OK-near-cap, and
an oversized explicit window) — since other/future consumers of the flag (e.g. a canonical-mapper
offload decision) rely on the stamp being present.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    return OpContext(
        workspace=Workspace(events=events, base_dir=tmp_path),
        events=events,
        permission_decl=PermissionDecl(),
        actor="test_skill",
    )


def _run(coro):
    return asyncio.run(coro)


# ── file.read stamps the flag on its self-bounding paths ──────────────────────────────────────


def test_unbounded_truncated_read_is_self_bounded(tmp_path: Path):
    """Tier 2: an unbounded read over the cap (truncated path) is stamped ``_self_bounded``."""
    (tmp_path / "big.py").write_text("x = 1\n" * 20000)  # well over the 8 KB floor
    res = _run(handle(FileIROp(kind="file", op="read", path="big.py"), _make_ctx(tmp_path)))
    assert res["status"] == "truncated" and "next_offset" in res
    assert res.get("_self_bounded") is True, "the truncated self-bounding read is flagged"


def test_unbounded_ok_read_is_self_bounded(tmp_path: Path):
    """Tier 2: an unbounded read whose content is ≤ cap (OK path) is also stamped ``_self_bounded``
    (the OK-near-cap edge — bounded by construction, not by truncation)."""
    (tmp_path / "small.py").write_text("hello = 1\n")
    res = _run(handle(FileIROp(kind="file", op="read", path="small.py"), _make_ctx(tmp_path)))
    assert res["status"] == "ok" and "next_offset" not in res
    assert res.get("_self_bounded") is True, "the OK unbounded read is flagged (bounded by construction)"


def test_oversized_explicit_window_read_is_self_bounded(tmp_path: Path):
    """Tier 2: owner steer — an OVERSIZED explicit-window file_read is SELF-BOUNDED (truncated),
    not verbatim. Supersedes the prior verbatim contract."""
    (tmp_path / "big.py").write_text("x = 1\n" * 20000)
    res = _run(handle(
        FileIROp(kind="file", op="read", path="big.py", offset=0, limit=20000),
        _make_ctx(tmp_path),
    ))
    assert res["status"] == "truncated", "an oversized explicit window is truncated, not verbatim"
    assert res["_self_bounded"] is True, "the oversized explicit-window read is self-bounded"
    assert res["_truncated"] is True, "LLM-visible truncation marker"
