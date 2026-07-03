"""Tier 2: #2296 — self-bounded file.read results are exempt from the generic control_ir offload.

file.read self-bounds its content ≤ the inline cap (#1209), but the generic offload triggered on the
whole-JSON size (content + envelope), so an already-bounded read was offloaded on ENVELOPE alone —
and retrieving it (an unbounded file.read of the ref) was itself over-cap on envelope → re-offloaded
→ infinite recursion. Fix: the read op stamps a positive ``_self_bounded`` flag on its self-bounding
paths; ``offload_control_ir_result`` exempts a flagged result before the size check. Per the later
owner steer (file_read never offload-duplicates its on-disk source), an OVERSIZED explicit window is
also self-bounded (truncated) now → ALL file reads are exempt, so a read can never start the offload
recursion.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.context_builder import offload_control_ir_result
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
        skill_name="test_skill",
    )


def _run(coro):
    return asyncio.run(coro)


# ── the offload exemption (unit) ──────────────────────────────────────────────────────────────


def test_self_bounded_result_exempt_from_offload(tmp_path: Path):
    """Tier 2: a result flagged ``_self_bounded`` whose serialized JSON exceeds the cap is returned
    UNCHANGED (no offload) — the over-cap-ness is envelope-only. RED without the exemption (it would
    be offloaded, gaining ``_offload_ref`` + a ``control_ir_result_offloaded`` event)."""
    events = EventLog()
    # content ≤ cap, but content + envelope > cap (the OK-near-cap edge).
    result = {"kind": "file", "op": "read", "path": "f.py", "status": "ok",
              "content": "a" * 190, "_self_bounded": True}
    out = offload_control_ir_result(result, 0, tmp_path, events=events, cap=200)
    assert out is result, "a self-bounded result is returned unchanged (identity)"
    assert "_offload_ref" not in out, "no offload ref added"
    assert [e for e in events.all() if e.type == "control_ir_result_offloaded"] == [], "no offload event"


def test_non_self_bounded_large_result_still_offloaded(tmp_path: Path):
    """Tier 2: the exemption is SCOPED to the flag — an equally-large result WITHOUT ``_self_bounded``
    (e.g. a python/MCP result) is still offloaded. Proves the fix doesn't disable offload broadly."""
    events = EventLog()
    result = {"kind": "python", "op": "exec", "status": "ok", "content": "a" * 190}
    out = offload_control_ir_result(result, 0, tmp_path, events=events, cap=200)
    assert out.get("_offload_ref"), "a non-self-bounded oversized result is offloaded"
    assert [e for e in events.all() if e.type == "control_ir_result_offloaded"], "offload event emitted"


# ── file.read stamps the flag on its self-bounding paths (integration) ────────────────────────


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


def test_oversized_explicit_window_read_is_self_bounded_and_exempt(tmp_path: Path):
    """Tier 2: owner steer — an OVERSIZED explicit-window file_read is now SELF-BOUNDED (truncated),
    so it is EXEMPT from the generic offload (no duplicate copy of an on-disk file). This strengthens
    the #2296 recursion-termination guarantee: a file_read never offloads → it can never start the
    offload/read recursion. Supersedes the prior verbatim-then-offloaded explicit-window contract."""
    (tmp_path / "big.py").write_text("x = 1\n" * 20000)
    res = _run(handle(
        FileIROp(kind="file", op="read", path="big.py", offset=0, limit=20000),
        _make_ctx(tmp_path),
    ))
    assert res["status"] == "truncated", "an oversized explicit window is truncated, not verbatim"
    assert res["_self_bounded"] is True, "the oversized explicit-window read is self-bounded"
    assert res["_truncated"] is True, "LLM-visible truncation marker"
    # and therefore it is NOT offloaded — no duplicate file for an already-on-disk source.
    out = offload_control_ir_result(res, 0, tmp_path, cap=200)
    assert "_offload_ref" not in out, "a self-bounded file_read is never offloaded (no duplicate copy)"


# ── recursion termination: a self-bounded read is never re-offloaded ──────────────────────────


def test_self_bounded_read_terminates_recursion(tmp_path: Path):
    """Tier 2: a real self-bounded read result (from file.read) passed to the offload is exempt →
    a retrieval read of any offloaded ref (unbounded → self-bounded) is not re-offloaded → the
    offload/read recursion terminates at the source (≤1 hop)."""
    (tmp_path / "big.py").write_text("x = 1\n" * 20000)
    res = _run(handle(FileIROp(kind="file", op="read", path="big.py"), _make_ctx(tmp_path)))
    assert res.get("_self_bounded") is True
    events = EventLog()
    out = offload_control_ir_result(res, 0, tmp_path, events=events, cap=200)  # tiny cap → would offload
    assert "_offload_ref" not in out, "the self-bounded read is exempt even under a tiny cap → no re-offload"
    assert [e for e in events.all() if e.type == "control_ir_result_offloaded"] == []
