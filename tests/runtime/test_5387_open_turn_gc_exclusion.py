"""Tier 2: #5387 "L" — a session's own history-content GC (#5364 §1.6 "C")
must not delete a file that belongs to the turn CURRENTLY triggering it —
"open-turn is GC-excluded by default, opt-in to include it" (owner
verbatim). Architect design B: ``not_open_turn(path) = recorded_chain(path)
!= current_chain`` — the triggering write's own ``chain_id`` IS "now" (GC
is writer-triggered), never approximated via mtime (two concurrent
sessions' writes interleave in mtime order — "oldest" is not "whose turn
is still open", see ``MediaStoreConfig.history_content_max_bytes``'s own
docstring).

Scope (architect design B, stated explicitly so this is not mistaken for
"we decided not to bother" — owner's own instruction, PR body carries the
same 3 lines): L protects ONLY the write that triggered THIS session's own
GC pass. Another session's own open turn is out of reach entirely — C only
ever enumerates THIS session's own directory. That stays unprotected until
a CROSS-session GC (#5366) is built; #5366 is where that would first need
to be handled, not here.
"""
from __future__ import annotations

import os

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.config.chat import OffloadConfig
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger


def _mcp_env(**data_extra) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "", "media_blocks": []}
    data.update(data_extra)
    return {"status": "ok", "data": data, "_canonical_source": "mcp"}


def _bump_all_mtimes_forward(directory) -> None:
    """Mirrors test_5364_history_content_cap_eviction.py's own helper —
    force every file already in *directory* one second further into the
    past so eviction order (sorted by mtime) is deterministic across a
    fast test loop."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


def test_the_write_time_cap_path_reaches_save_tool_result_with_a_real_chain_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5387 wiring witness — drives the REAL production chain
    (``RouterLoop.feedback`` -> ``RouterHostAdapter.cap_tool_result`` ->
    ``Session._cap_tool_result`` -> ``ContextBudgetAdvisor.cap_tool_result``
    -> ``tool_result_cap.cap_tool_result_content`` -> real
    ``MediaStore.save_tool_result``, mirroring
    test_5364_spilled_meta_wiring.py's own
    ``test_the_real_production_chain_stamps_spilled_meta_end_to_end`` real-
    chain harness, no mocks/fakes at any of those 5 hops) with a real,
    non-empty ``chain_id`` and asserts the write LANDED with that chain_id
    recorded — via the PUBLIC ``MediaStore.is_open_turn_file`` seam, never
    the private ``_chain_by_path`` dict directly.

    Strip-falsify: removing ``chain_id=chain_id``/``chain_id=self.chain_id``
    from ANY of this chain's forwarding call sites (tool_result_cap.py's
    own ``save_fn`` call, context_budget_advisor.py's ``cap_tool_result``,
    session.py's ``_cap_tool_result``, router_host_adapter.py's
    ``cap_tool_result``, or router_loop.py's own two ``_cap(...)`` calls)
    makes this go RED — the store never learns the chain, so
    ``is_open_turn_file`` reports False for a real chain_id that was
    genuinely used to write it."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="real-chain-agent",
        multimodal_config=MultimodalConfig(),
        offload_config=OffloadConfig(enabled=True),
        compaction_config=CompactionConfig(use_chars4_estimate=True),
    )

    loop = RouterLoop(host=session.router_host, chain_id="the-real-chain", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[_mcp_env(content=_BIG)],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    loop.feedback(result)

    (tool_msg,) = [m for m in session.history if m.role == "tool"]
    ref = (tool_msg.meta or {}).get("content_ref")
    assert ref, f"test setup sanity: expected an offloaded content ref, meta={tool_msg.meta!r}"
    full_path = (tmp_path / ref).resolve()
    store = session._media_store
    assert store is not None, "test setup sanity: session must have a real MediaStore"
    assert store.is_open_turn_file(full_path, current_chain_id="the-real-chain"), (
        "the write-time cap path's write must have reached save_tool_result "
        "with the real chain_id — is_open_turn_file reports False, meaning "
        "the chain never landed"
    )


def test_a_file_from_a_different_chain_is_not_protected_by_the_current_one(
    tmp_path,
) -> None:
    """Tier 2: #5387 "turn boundary" witness — protection is ZERO outside
    the triggering turn, rejecting an "always protects everything"
    implementation that would pass the wiring test above for the wrong
    reason. Two files, two DIFFERENT chains; eviction triggered by chain
    "c1" must protect ONLY the "c1" file and evict the "c2" one (out of
    "c1"'s own turn) exactly as it would any ordinary evictable file."""
    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=50),
        project_root=tmp_path,
        agent_name="alice",
        session_id="main",
    )

    old_block = store.save_tool_result(
        "payload from chain two " * 2, mime_type="text/plain", chain_id="c2", seq=1,
    )
    _bump_all_mtimes_forward(store.history_content_dir)
    # The triggering write itself: chain_id="c1" — its own presence pushes
    # the directory over the tiny cap, so THIS call's own
    # _evict_history_content_over_cap(current_chain_id="c1") is exactly
    # the pass under test.
    new_block = store.save_tool_result(
        "payload from chain one " * 2, mime_type="text/plain", chain_id="c1", seq=2,
    )

    old_path = tmp_path / old_block["path"]
    new_path = tmp_path / new_block["path"]
    assert not old_path.exists(), (
        "a file from a DIFFERENT chain than the one triggering GC must be "
        "evicted like any ordinary file — protection does not extend past "
        "the triggering turn"
    )
    assert new_path.exists(), (
        "the file belonging to THIS pass's own triggering chain must survive"
    )


def test_the_older_of_two_same_chain_files_survives_only_because_of_the_exclusion(
    tmp_path,
) -> None:
    """Tier 2: #5387 architect review — the sibling tests above (turn-
    boundary, opt-in) are each ALSO fully explained by oldest-first
    ordering alone, with zero exclusion in effect: the triggering
    chain's own write is always the NEWEST file, so "the newest survives"
    never distinguishes "protected" from "just not old enough to be
    picked yet". This is the one construction where ordering alone
    predicts the OPPOSITE outcome from exclusion, so exclusion is the
    ONLY explanation left: two files from the SAME chain ("c1"), the
    flag at its default (True) — an oldest-first-only implementation
    would evict the older one (it is not the newest); the exclusion
    must keep BOTH, since both share the chain that is triggering THIS
    pass.

    Strip-falsify: removing the ``protect_open_turn`` skip from
    ``_evict_history_content_over_cap`` makes ONLY this test go RED (the
    3 sibling tests all stay green — proof they were never
    distinguishing this case)."""
    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=50),
        project_root=tmp_path,
        agent_name="alice",
        session_id="main",
    )

    older_block = store.save_tool_result(
        "payload number one " * 2, mime_type="text/plain", chain_id="c1", seq=1,
    )
    _bump_all_mtimes_forward(store.history_content_dir)
    # The triggering write itself — same chain as the older file above,
    # so BOTH must be recognized as "this pass's own open turn".
    newer_block = store.save_tool_result(
        "payload number two " * 2, mime_type="text/plain", chain_id="c1", seq=2,
    )

    older_path = tmp_path / older_block["path"]
    newer_path = tmp_path / newer_block["path"]
    assert older_path.exists(), (
        "the OLDER file must survive — an oldest-first-only "
        "implementation (no exclusion) would evict it, since it is "
        "not the newest file; the exclusion is what keeps it, because "
        "it shares the SAME chain as the write triggering this pass"
    )
    assert newer_path.exists()


def test_protect_open_turn_from_gc_false_opts_the_current_chain_back_into_eviction(
    tmp_path,
) -> None:
    """Tier 2: #5387 opt-in witness (owner verbatim: "既定はそうして、opt-in
    で対象にもするようにしたい") — with the flag explicitly disabled, even the
    triggering chain's own file is evictable, oldest-first, same as before
    #5387 existed."""
    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=50, protect_open_turn_from_gc=False),
        project_root=tmp_path,
        agent_name="alice",
        session_id="main",
    )

    first_block = store.save_tool_result(
        "payload number one " * 2, mime_type="text/plain", chain_id="c1", seq=1,
    )
    _bump_all_mtimes_forward(store.history_content_dir)
    second_block = store.save_tool_result(
        "payload number two " * 2, mime_type="text/plain", chain_id="c1", seq=2,
    )

    first_path = tmp_path / first_block["path"]
    second_path = tmp_path / second_block["path"]
    assert not first_path.exists(), (
        "opt-in (protect_open_turn_from_gc=False) must let the SAME chain's "
        "own earlier file be evicted too, oldest-first, exactly as if it "
        "carried no chain_id at all"
    )
    assert second_path.exists()
