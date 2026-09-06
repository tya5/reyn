"""Tier 2: #5896 stage ① — #5364 §1.1 "A" delivered at RETURN time: every
tool result's body is written once to ``history-content/`` through the
one write seam (``MediaStore.save_tool_result``), the ``history.jsonl``
row carries the ref and no body, and everything a reader sees is
byte-identical to before (the stage's own witness).

Owner (#5364 §1.1 A, quoted in #5896): "常に file 化する — history.jsonl は
参照だけを持つ". Architect (#5896 ruling): the site is the tool-row append
seam; write-ahead ("ref を持つ row の file は append 時点で存在する");
§1.5 stays the one inline exception; and — stage ① condition — "GC は
un-spilled の file を候補にしない", without which ``build_history``'s
byte-identity across a restart cannot hold.

Real ``Session``/``MediaStore``/``DurabilityWorker`` throughout (the
``tests/_support/router_host_adapter.py`` real-collaborators contract);
the ONE ``FakeRouterHost`` here observes the append ORDER, which no real
collaborator exposes as state (see that test's own docstring).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reyn.config import MultimodalConfig
from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.config.infra import StorageConfig
from reyn.core.events.durability_worker import DurabilityWorker
from reyn.core.events.state_log import StateLog
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.chat_message import (
    CONTENT_BYTES_META_KEY,
    CONTENT_REF_META_KEY,
    LOST_REASON_META_KEY,
    SPILLED_META_KEY,
    LostReason,
)
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session
from tests._support.router_loop import FakeRouterHost

_MODEL = "gpt-4o"
# Large enough that "the body is not on the history.jsonl line" is a real
# claim, and — offload stays at its shipped default (off) — never capped:
# A is a storage form, not a size gate.
_BODY = "\n".join(f"line {i}: " + "y" * 60 for i in range(3000))
_SMALL = "a small result that stays well under any cap"


def _mcp_env(content: str) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": content, "media_blocks": []}
    return {"status": "ok", "data": data, "_canonical_source": "mcp"}


def _round(content: str) -> ExecutionResult:
    return ExecutionResult(
        tool_results=[_mcp_env(content)],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )


def _session(agent_name: str, tmp_path: Path, **kwargs):
    return make_session(
        agent_name=agent_name,
        multimodal_config=MultimodalConfig(),
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}.snap.json",
        safety=SafetyConfig(loop=LoopConfig(), on_limit=OnLimitConfig(mode="unattended")),
        **kwargs,
    )


def _durable_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _bump_all_mtimes_forward(directory: Path) -> None:
    """Mirrors tests/data/test_5364_history_content_cap_eviction.py's own
    helper — push every file already in *directory* one second into the
    past so "oldest" is unambiguous inside a fast test loop."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


@pytest.mark.asyncio
async def test_a_returned_tool_result_is_file_backed_and_its_durable_row_has_no_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ① + ⑧ of the #5896 ruling, through the real
    production chain (``RouterLoop.feedback`` → ``persist_feedback`` →
    ``RouterHostAdapter.append_history_entry`` → ``Session._append_history``
    → ``history_record``): the resident row keeps the body (the live
    cache), its meta names a file that holds that SAME body, the
    ``history.jsonl`` line carries NO body, and the body is on disk
    exactly once — in the file, never in the log.

    Strip-falsify: remove ``history_record``'s body drop (persist
    ``asdict`` again) → the body appears in ``history.jsonl`` → red;
    remove ``feedback``'s ``save_tool_result`` call → no ref, no file →
    red."""
    monkeypatch.chdir(tmp_path)
    session = _session("return-time-agent", tmp_path)
    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)

    loop.feedback(_round(_BODY))
    await loop.persist_feedback()

    (tool_msg,) = [m for m in session.history if m.role == "tool"]
    assert _BODY in tool_msg.text, "the resident row is the live cache — it keeps the body"
    meta = tool_msg.meta
    assert meta.get(SPILLED_META_KEY) is False, f"un-spilled: the model sees the body inline, meta={meta!r}"
    ref = meta.get(CONTENT_REF_META_KEY)
    assert ref, f"the row must name its file, meta={meta!r}"
    body_file = tmp_path / ref
    assert body_file.is_file()
    assert body_file.read_text(encoding="utf-8") == tool_msg.text
    assert meta.get(CONTENT_BYTES_META_KEY) == len(tool_msg.text.encode("utf-8"))

    (durable_row,) = [r for r in _durable_rows(session.history_path) if r["role"] == "tool"]
    assert durable_row["content"] == "", "history.jsonl は参照だけを持つ — no body on the line"
    assert durable_row["meta"][CONTENT_REF_META_KEY] == ref
    assert _BODY not in session.history_path.read_text(encoding="utf-8")
    holders = [
        p for p in tmp_path.rglob("*")
        if p.is_file() and _BODY in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert holders == [body_file], f"the body must be on disk exactly once, found in: {holders}"


@pytest.mark.asyncio
async def test_build_history_wire_is_the_same_live_and_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ② — the strongest witness that A is a storage
    form only (architect: "build_history の出力が A の前後で byte 一致"):
    the wire the model would see from the LIVE session (resident rows,
    body cached) equals the wire a SECOND session rebuilds from disk
    alone (rows parsed from ``history.jsonl``, body hydrated from the
    file by ``Session._parse_history_line`` through the one resolver).

    This is also acceptance ③'s restart half: the on-disk state the
    second session reads is exactly what a kill right after
    ``persist_feedback`` leaves behind (nothing else runs between the
    append and this test's own restart), and the body resolves — it is
    not ``lost``. The restored ``session.history`` row itself holds the
    body, which is what ``project_restored_frames`` and the read model
    project without any change of their own.

    Strip-falsify: remove the hydration in ``_parse_history_line`` → the
    restarted wire carries an empty tool body → red."""
    monkeypatch.chdir(tmp_path)
    live = _session("restart-agent", tmp_path)
    loop = RouterLoop(host=live.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(_round(_BODY))
    await loop.persist_feedback()
    live_wire = live._loop_driver._history_buffer.build_history()
    (live_tool,) = [m for m in live_wire if m.get("role") == "tool"]
    assert _BODY in live_tool["content"], "test setup sanity: the live wire carries the body"

    restarted = _session("restart-agent", tmp_path)
    restarted.load_history()

    (restored_row,) = [m for m in restarted.history if m.role == "tool"]
    (live_row,) = [m for m in live.history if m.role == "tool"]
    restarted_wire = restarted._loop_driver._history_buffer.build_history()
    assert restored_row.text == live_row.text
    assert restarted_wire == live_wire


@pytest.mark.asyncio
async def test_the_ref_file_exists_before_its_row_is_appended(tmp_path: Path) -> None:
    """Tier 2: acceptance ③'s ordering half (#5364 §1.4 write-ahead) —
    ``persist_feedback`` flushes the store's off-loop write BEFORE it
    replays the queued appends, so at the moment ``append_history_entry``
    is called for a tool row, the file its ref names already exists.

    The order of two calls is not state any real collaborator keeps —
    a real ``Session`` records only the outcome. So the host here is
    ``FakeRouterHost`` (the sibling of ``test_5364_media_store_flush_
    barrier.py``'s own ``_MediaStoreHost``) with a REAL ``MediaStore``:
    its ``append_history_entry`` looks at the filesystem AT THE CALL, the
    one observation the invariant is about. Runs inside a loop so the
    store's write is genuinely deferred to the worker (a no-loop run
    would write inline and pass without any ordering at all).

    Strip-falsify: swap the flush after the replay in
    ``persist_feedback`` → the write is still queued when the row lands
    → ``False`` recorded → red. Also witnesses that ``feedback`` itself
    appends nothing (the rows are held back until the flush)."""
    store = MediaStore(project_root=tmp_path, agent_name="order-agent", session_id="main")

    class _AppendObservingHost(FakeRouterHost):
        def __init__(self) -> None:
            super().__init__()
            self.media_store = store
            self.file_existed_at_append: list[bool] = []

        def append_history_entry(self, *, role, content, meta=None, **kwargs) -> None:
            if role == "tool":
                ref = (meta or {}).get(CONTENT_REF_META_KEY)
                self.file_existed_at_append.append(bool(ref) and (tmp_path / ref).is_file())
            super().append_history_entry(role=role, content=content, meta=meta, **kwargs)

    host = _AppendObservingHost()
    loop = RouterLoop(host=host, chain_id="c1", router_model=_MODEL)

    loop.feedback(_round(_SMALL))
    assert host.history == [], "feedback() queues; nothing reaches history before the flush"

    await loop.persist_feedback()

    assert host.file_existed_at_append == [True]
    assert [e["role"] for e in host.history] == ["assistant", "tool"], "persisted in wire order"


@pytest.mark.asyncio
async def test_a_latched_write_failure_keeps_the_returned_body_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ④ — #5364 §1.5 on the RETURN-time path (the
    spill path's own §1.5 witness is test_5364_write_unavailable_keeps_
    content_inline.py, untouched): once the real ``DurabilityWorker`` has
    latched ``durability_failed``, ``save_tool_result`` refuses, and the
    row keeps its body INLINE on the durable line too — no ref is ever
    minted for a file that will not exist — with the same
    ``LostReason.NEVER_PERSISTED`` stamp the spill path's refusal uses
    (one branch, not two). Strip-falsify: catch the refusal and stamp a
    ref anyway → ``CONTENT_REF_META_KEY`` present → red."""
    monkeypatch.chdir(tmp_path)
    worker = DurabilityWorker(max_write_attempts=2, retry_base_s=0.001, retry_max_s=0.005)
    session = _session("latched-agent", tmp_path, media_store_worker=worker)

    async def _always_fail() -> None:
        raise OSError("disk full")

    worker.submit_nowait(_always_fail)
    await worker.flush()
    assert session.router_host.media_store.durability_failed is True, "test setup sanity"

    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(_round(_SMALL))
    await loop.persist_feedback()

    (tool_msg,) = [m for m in session.history if m.role == "tool"]
    assert CONTENT_REF_META_KEY not in tool_msg.meta
    assert SPILLED_META_KEY not in tool_msg.meta
    assert tool_msg.meta.get(LOST_REASON_META_KEY) == LostReason.NEVER_PERSISTED
    (durable_row,) = [r for r in _durable_rows(session.history_path) if r["role"] == "tool"]
    assert _SMALL in durable_row["content"], "§1.5: the body stays inline on the durable line"


def test_gc_never_selects_an_unspilled_file(tmp_path: Path) -> None:
    """Tier 2: the stage ① condition (architect, #5896): with the
    per-session cap narrowed to less than one file, two un-spilled writes
    both survive — the pass deletes nothing, over cap — while a spilled
    write is still evicted as before. The exclusion survives a restart
    (a NEW store reads it back from the manifest) and a manifest prune
    (the third store below loads a manifest the second one rewrote after
    finding the evicted spill's line stale). Public seam:
    ``MediaStore.is_unspilled_file``. Strip-falsify: drop the
    ``is_unspilled_file`` skip in ``_evict_history_content_over_cap`` →
    the oldest un-spilled file is evicted → red."""
    config = MediaStoreConfig(history_content_max_bytes=50)
    store = MediaStore(config, project_root=tmp_path, agent_name="alice", session_id="main")

    first = store.save_tool_result("payload number 0 " * 2, seq=0, spilled=False)
    assert (tmp_path / first["path"]).exists()
    _bump_all_mtimes_forward(store.history_content_dir)
    second = store.save_tool_result("payload number 1 " * 2, seq=1, spilled=False)
    assert (tmp_path / second["path"]).exists()
    _bump_all_mtimes_forward(store.history_content_dir)
    assert (tmp_path / first["path"]).exists(), "over cap, but an un-spilled body is never a candidate"

    spilled = store.save_tool_result("payload number 2 " * 2, seq=2)
    assert not (tmp_path / spilled["path"]).exists(), (
        "the spilled write is the ONLY candidate, so it is the one evicted"
    )
    assert (tmp_path / first["path"]).exists()
    assert (tmp_path / second["path"]).exists()

    reopened = MediaStore(config, project_root=tmp_path, agent_name="alice", session_id="main")
    assert reopened.is_unspilled_file(first["path"])
    assert reopened.is_unspilled_file(second["path"])
    assert not reopened.is_unspilled_file(spilled["path"])
    reopened_again = MediaStore(config, project_root=tmp_path, agent_name="alice", session_id="main")
    assert reopened_again.is_unspilled_file(first["path"]), (
        "the prune-rewrite must keep the spilled flag, or a restart re-admits the file to GC"
    )


def test_cross_session_gc_never_lists_an_unspilled_file(tmp_path: Path) -> None:
    """Tier 2: the same condition on the PROJECT-wide pass (#5366's
    ``storage.max_bytes``): another session's store, well over the
    project cap, previews no eviction of alice's un-spilled bodies — the
    manifest is project-wide, so bob's store knows them — while alice's
    later spilled write does appear. Strip-falsify: drop the
    ``unspilled`` filter in ``cross_session_eviction_candidates`` → the
    preview lists alice's un-spilled files → red."""
    alice = MediaStore(project_root=tmp_path, agent_name="alice", session_id="main")
    kept = alice.save_tool_result("payload number 0 " * 2, seq=0, spilled=False)
    _bump_all_mtimes_forward(alice.history_content_dir)

    bob = MediaStore(
        project_root=tmp_path, agent_name="bob", session_id="main",
        storage=StorageConfig(max_bytes=10),
    )
    assert bob.cross_session_eviction_preview() == []

    evictable = alice.save_tool_result("payload number 1 " * 2, seq=1)
    preview = MediaStore(
        project_root=tmp_path, agent_name="bob", session_id="main",
        storage=StorageConfig(max_bytes=10),
    ).cross_session_eviction_preview()
    assert (tmp_path / evictable["path"]).resolve() in {p.resolve() for p in preview}
    assert (tmp_path / kept["path"]).resolve() not in {p.resolve() for p in preview}
