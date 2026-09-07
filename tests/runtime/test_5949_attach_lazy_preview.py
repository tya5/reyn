"""Tier 2: #5949 (owner-hit P0) — backward-paging (attach's own
``extend_history_backward``/``extend_history_backward_async``, TUI
scrollback paging) must not eager-materialize a content_ref row's full
body. Owner-hit incident: attach eagerly hydrated ~566 MB across 44 rows
in the tail 200 lines, ballooning an 8226 MB -> 91 MB migration win right
back to ~8-10 GB. Stage ① of architect's 4-point ruling (issue #5949):
the display/backward-paging path gets a BOUNDED preview instead, stored in
a SEPARATE meta field, never touching ``.content`` itself — which is what
keeps the LLM wire path (``RouterHistoryBuffer._serialise_turn``, already
lazily resolving) completely unaffected.

Real ``Session``/``RouterLoop``/``MediaStore`` throughout — a genuine
content_ref row is produced via the SAME production write seam
(``RouterLoop.feedback`` -> ``persist_feedback``) #5896's own acceptance
tests use, never a hand-built row for the rows under direct test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.config import MultimodalConfig
from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import (
    CONTENT_PREVIEW_META_KEY,
    CONTENT_REF_META_KEY,
    SPILL_TARGET_CONTENT_HASH_META_KEY,
    SPILLED_META_KEY,
)
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"
_BODY = "y" * 20_000  # comfortably over the preview bound, well under a real incident row


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


async def _write_one_content_ref_row(tmp_path: Path, agent_name: str, body: str) -> None:
    """Writes ONE real content_ref tool row via the production seam, then
    discards the session object — only the durable history.jsonl + its
    history-content file matter to the tests below, which reload fresh."""
    session = _session(agent_name, tmp_path)
    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(_round(body))
    await loop.persist_feedback()


async def _write_content_ref_row_then_filler_turns(
    tmp_path: Path, agent_name: str, body: str, *, n_filler_turns: int,
) -> None:
    """Like :func:`_write_one_content_ref_row`, plus *n_filler_turns* plain
    user/assistant turns appended AFTER it — pushes the content_ref row far
    enough into the past that a fresh session's own bounded in-memory tail
    (mirroring ``test_5139c_older_backlog_wire_roundtrip.py``'s own
    ``session.history = session.history[-2:]`` idiom) genuinely excludes
    it, forcing a real on-disk extend to bring it back — needed for
    :func:`test_attach_endpoint_backlog_page_never_eager_hydrates_via_the_
    async_path` below, which must drive the row through a REAL disk
    extend, not just a freshly-loaded in-memory tail that happens to
    already contain it."""
    from reyn.runtime.chat_message import ChatMessage

    session = _session(agent_name, tmp_path)
    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(_round(body))
    await loop.persist_feedback()
    for i in range(n_filler_turns):
        session._append_history(ChatMessage(role="user", content=f"filler question {i}"))  # noqa: SLF001 - real durable-write seam, same as test_5139c's own helper
        session._append_history(ChatMessage(role="assistant", content=f"filler answer {i}"))  # noqa: SLF001


# ── 1. backward-paging never eager-hydrates .content ────────────────────


@pytest.mark.asyncio
async def test_backward_paged_content_ref_row_keeps_content_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the core claim — a content_ref row loaded via
    ``extend_history_backward`` (the same primitive ``_load_older_entries``
    both attach and TUI scrollback paging use) must NOT have its full body
    materialized into ``.content``/`.text`` — this is the whole point:
    materializing it is exactly what blew up 566 MB -> 8 GB.

    Strip witness: passing ``hydrate=True`` (the pre-①-b default polarity)
    from ``_load_older_entries`` makes ``.text`` equal the full 20000-char
    body instead of empty — verified directly, restored after."""
    monkeypatch.chdir(tmp_path)
    await _write_one_content_ref_row(tmp_path, "attach-agent", _BODY)

    restarted = _session("attach-agent", tmp_path)
    restarted.load_history()
    (loaded_seq,) = [m.seq for m in restarted.history if m.role == "tool"]
    # Force a real backward page even though the whole (tiny, 1-turn)
    # history already loaded at startup — mirrors the real shape (a
    # caller asking for "more"). Calls the private primitive directly
    # with an explicit before_seq (one past the row's own seq) rather
    # than the public wrapper's own `self.history[0].seq if self.history
    # else 0` auto-derivation, which cannot express "everything, from an
    # empty resident set" — the same "simulate a bounded resident set"
    # idiom test_4387_history_resident_eviction.py's own sibling tests
    # use, just via direct seq rather than list-slicing.
    restarted.history = []
    prepended = restarted._load_older_entries(before_seq=loaded_seq + 1, min_lines=10)

    assert prepended >= 1, "sanity: the backward page must have found the tool row"
    (tool_msg,) = [m for m in restarted.history if m.role == "tool"]
    assert tool_msg.text == "", (
        f"a backward-paged content_ref row must NOT have its body materialized "
        f"into .content; got {len(tool_msg.text)} chars"
    )
    assert tool_msg.meta.get(CONTENT_REF_META_KEY), "sanity: this must genuinely be a content_ref row"


# ── 1b. the REAL owner-driving path: endpoint.session_backlog_page ──────


@pytest.mark.asyncio
async def test_attach_endpoint_backlog_page_never_eager_hydrates_via_the_async_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5949 stage ①-b acceptance (owner-hit P0, architect
    structural ruling issuecomment-5576144700) — drives the REAL path
    attach actually uses (``endpoint.session_backlog_page`` ->
    ``Session.extend_history_backward_async``, via ``endpoint.py``'s
    backlog send), never ``_load_older_entries``/the private primitive
    directly. Stage ① alone left THIS path unfixed: it had its own,
    separate parse loop with no ``hydrate`` awareness at all, so owner's
    real backward-page-heavy path kept eager-materializing every
    content_ref body, unchanged, after stage ① landed — owner's real
    measurement got WORSE (11 GB / 14 GB peak), not better. This is the
    acceptance lead-coder's own review named as missing from stage ①'s
    TESTS-READ: "test は attach の経路を driving していませんでした."

    Strip witness: reverting :meth:`Session.extend_history_backward_async`
    to its pre-①-b form (its own separate
    ``self._parse_history_line(line)`` call, the eager-hydrate-by-default
    polarity ``hydrate`` replaced) turns this red — verified directly,
    restored after."""
    from reyn.interfaces.transport.agui.endpoint import session_backlog_page
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry

    monkeypatch.chdir(tmp_path)
    agent_name = "endpoint-agent"
    await _write_content_ref_row_then_filler_turns(
        tmp_path, agent_name, _BODY, n_filler_turns=5,
    )

    def factory(profile: AgentProfile):
        s = _session(profile.name, tmp_path)
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create(agent_name)
    try:
        await reg.attach(agent_name)
        session = reg.get_session(agent_name, _DEFAULT_SID)
        assert session is not None
        (loaded_seq,) = [m.seq for m in session.history if m.role == "tool"]
        # Bound the in-memory tail BELOW the tool row -- forces a REAL
        # on-disk extend (mirrors test_5139c_older_backlog_wire_roundtrip.
        # py's own test_has_more_extends_from_disk_past_the_in_memory_tail
        # idiom) rather than letting a freshly-loaded tail happen to
        # already contain the row under test.
        session.history = [m for m in session.history if m.seq > loaded_seq]
        assert session.history, "sanity: filler turns must still be resident"
        assert not any(m.role == "tool" for m in session.history), (
            "sanity: the tool row must genuinely be excluded from the "
            "bounded in-memory tail, or the extend below proves nothing"
        )

        await session_backlog_page(reg, agent_name, _DEFAULT_SID)

        (tool_msg,) = [m for m in session.history if m.role == "tool"]
        assert tool_msg.content == "", (
            f"a content_ref row pulled back via the REAL attach/endpoint "
            f"path (session_backlog_page -> extend_history_backward_async) "
            f"must NOT have its body materialized; got "
            f"{len(tool_msg.content)} chars"
        )
        assert tool_msg.meta.get(CONTENT_PREVIEW_META_KEY), (
            "a bounded preview must still be filled for display, via the "
            "same _fill_content_previews pass the sync path uses"
        )
    finally:
        await reg.shutdown()


def test_preview_only_parse_leaves_content_empty_directly(tmp_path: Path) -> None:
    """Tier 1: the same claim as above, isolated to ``_parse_history_line``
    itself (no Session-level paging machinery involved) — a durable line
    with a content_ref, parsed with ``hydrate=False``, returns a message
    whose ``.content`` is still ``""``.

    Strip witness: removing the ``if not hydrate: return msg`` guard
    (falling through to the eager resolve) makes ``.content`` equal the
    full body — verified directly, restored after."""
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig

    session = _session("preview-only-agent", tmp_path)
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path,
        agent_name="preview-only-agent", session_id="s1",
    )
    session._media_store = store
    ref_block = store.save_tool_result(_BODY, tool="t", seq=1)
    line = json.dumps({
        "role": "tool", "content": "", "ts": "", "seq": 1,
        "meta": {CONTENT_REF_META_KEY: ref_block["path"], SPILLED_META_KEY: False},
        "tool_calls": None, "tool_call_id": None, "name": "t",
        "spillability": "last_resort", "disclosure": None,
    })

    msg = session._parse_history_line(line, hydrate=False)

    assert msg is not None
    assert msg.content == "", f"hydrate=False must leave content empty, got {len(msg.content)} chars"


# ── 2. a bounded preview is filled, with the "+N MB" notice ─────────────


@pytest.mark.asyncio
async def test_fill_content_previews_produces_a_bounded_head_plus_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the preview must be a BOUNDED head of the real body plus a
    "+N MB" notice when the body exceeds the bound — not empty, not the
    full body (architect's own ruling: "先頭N byte＋…(+N MB)")."""
    monkeypatch.chdir(tmp_path)
    big = "z" * 50_000  # well over the preview bound
    await _write_one_content_ref_row(tmp_path, "preview-agent", big)

    restarted = _session("preview-agent", tmp_path)
    restarted.load_history()
    (loaded_seq,) = [m.seq for m in restarted.history if m.role == "tool"]
    restarted.history = []
    restarted._load_older_entries(before_seq=loaded_seq + 1, min_lines=10)

    (tool_msg,) = [m for m in restarted.history if m.role == "tool"]
    preview = tool_msg.meta.get(CONTENT_PREVIEW_META_KEY)
    assert preview, "a preview must be filled for a content_ref row"
    assert preview.startswith("z" * 100), "the preview must start with the real head of the body"
    assert len(preview) < len(big), "the preview must be strictly bounded, not the full body"
    assert "MB)" in preview, 'the preview must carry the "...(+N MB)" notice when truncated'


# ── 3. the sibling wire-assert: build_history is unaffected ─────────────


@pytest.mark.asyncio
async def test_build_history_still_carries_the_full_body_after_backward_paging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: THE load-bearing sibling assert (lead-coder's own explicit
    requirement) — without this, "removed the waste" and "broke the
    feature" are indistinguishable. A row loaded via backward-paging
    (content empty, preview-only) must STILL serialise its FULL body onto
    the LLM wire when build_history() actually needs it — proving the
    preview mechanism never substitutes for the real content at the one
    place that matters."""
    monkeypatch.chdir(tmp_path)
    await _write_one_content_ref_row(tmp_path, "wire-agent", _BODY)

    restarted = _session("wire-agent", tmp_path)
    restarted.load_history()
    (loaded_seq,) = [m.seq for m in restarted.history if m.role == "tool"]
    restarted.history = []
    restarted._load_older_entries(before_seq=loaded_seq + 1, min_lines=10)

    (tool_msg,) = [m for m in restarted.history if m.role == "tool"]
    assert tool_msg.text == "", "sanity: still preview-only at this point, not the full body"

    wire = restarted._loop_driver._history_buffer.build_history()
    (wire_tool,) = [m for m in wire if m.get("role") == "tool"]
    assert _BODY in wire_tool["content"], (
        "build_history's own wire output must still carry the FULL body — "
        "the preview mechanism must never leak into (or substitute for) "
        "what actually reaches the LLM"
    )


# ── 4. spill_record rows are never hydrate/preview candidates ───────────


def test_spill_record_row_is_never_given_a_preview(tmp_path: Path) -> None:
    """Tier 1: a spill_record row (the durable supersede ledger, #5612) —
    constructed exactly as ``RouterHistoryBuffer.spill_turn_content``
    actually produces one (``SPILLED_META_KEY: True``, non-empty content —
    its own small offloaded preview text, never emptied by
    ``history_record``, see that function's own docstring) — must never be
    treated as a preview candidate.

    Two of ``_fill_content_previews``'s guard conditions independently
    exclude a REAL spill_record row this way — ``msg.content != ""``
    (true for every real one) and ``meta.get(SPILLED_META_KEY)`` (also
    always true for one) — measured directly while building this test: an
    edge-case row with content forced empty AND ``role`` changed away from
    ``spill_record`` STILL did not leak a preview, because the SPILLED
    check alone still caught it. This test targets the REALISTIC shape,
    not an attempt to isolate one guard in artificial isolation.

    Strip witness: removing the entire guard clause (content-emptiness
    AND spilled-check together) lets a real-shaped spill_record row
    through — verified directly, restored after."""
    from reyn.runtime.chat_message import ChatMessage

    session = _session("spill-record-agent", tmp_path)
    spill_row = ChatMessage(
        role="spill_record",
        content="a small offloaded preview, never empty — matches real construction",
        meta={
            SPILLED_META_KEY: True,
            CONTENT_REF_META_KEY: "some/ref/path.txt",
            SPILL_TARGET_CONTENT_HASH_META_KEY: "sha256:deadbeef",
        },
    )

    session._fill_content_previews([spill_row])

    assert CONTENT_PREVIEW_META_KEY not in spill_row.meta, (
        "a spill_record (ledger) row must never be given a preview — it "
        "never needs a body materialized for it at all"
    )


# ── 5. the same ref is read at most once per _load_older_entries pass ───


@pytest.mark.asyncio
async def test_same_ref_is_read_once_per_pass_even_with_two_referencing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: architect's own measured finding — a ``tool`` row and its
    own ``spill_record`` can point at the SAME backing file. Even though
    the spill_record itself is excluded (test 4 above), TWO ``tool`` rows
    pointing at the identical ref (a realistic shape — the same content
    hash, offloaded once, referenced by more than one row) must still
    only cost ONE real file read per ``_load_older_entries`` call, not one
    per referencing row.

    Strip witness: removing the ``if ref not in cache:`` guard in
    ``_fill_content_previews`` (always calling ``_build_content_preview``)
    makes the real-read call count 2 instead of 1 — verified directly,
    restored after."""
    monkeypatch.chdir(tmp_path)
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig

    session = _session("dedup-agent", tmp_path)
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="dedup-agent", session_id="s1",
    )
    ref_block = store.save_tool_result(_BODY, tool="t", seq=1)
    ref = ref_block["path"]
    lines = [
        json.dumps({
            "role": "tool", "content": "", "ts": "", "seq": seq,
            "meta": {CONTENT_REF_META_KEY: ref, SPILLED_META_KEY: False},
            "tool_calls": None, "tool_call_id": None, "name": "t",
            "spillability": "last_resort", "disclosure": None,
        })
        for seq in (1, 2)
    ]
    session.history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    session._media_store = store

    calls: "list[str]" = []
    original = MediaStore.read_tool_result_preview

    def _counting(self: MediaStore, path_str: str, *, max_bytes: int):
        calls.append(path_str)
        return original(self, path_str, max_bytes=max_bytes)

    monkeypatch.setattr(MediaStore, "read_tool_result_preview", _counting)

    prepended = session._load_older_entries(before_seq=999, min_lines=10)

    assert prepended == 2, "sanity: both rows sharing the same ref must still be loaded"
    assert calls == [ref], (
        f"the same ref must be read exactly once across both referencing rows; "
        f"got {len(calls)} real read(s): {calls!r}"
    )
