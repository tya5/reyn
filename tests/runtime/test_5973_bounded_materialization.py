"""Tier 2: #5973 (owner-hit P0, architect's structural ruling) — a
content_ref row's REAL, potentially-huge body must never be pulled into
memory unconditionally, at three separate places that used to each pull
it in without limit:

  ① ``Session._evict_oldest_resident_entries`` (the ``history_resident``
     bound) must count what a resident content_ref row can actually pull
     in (its BodyBytes), not the ~400-byte serialized shell
     (``ChatMessage.resident_bytes()``) #5896's migration left it
     counting instead — the SAME cap that measured ~400 bytes/row for an
     unbounded backlog of huge tool results, bounding nothing.
  ② ``RouterHistoryBuffer._serialise_turn`` (the wire-build path,
     ``build_history``/``decompose_history_for_retry``) must not
     materialize a content_ref row's body past a real budget — real
     audit-event trace: a single ``hello`` send re-resolved a 597 MB
     backlog whole, every turn, every call (issuecomment-5577574709).
  ③ ``Session._durable_active_history_after`` (compaction's own
     candidate read, the reactive-overflow RECOVERY path) must hydrate
     candidates under the SAME budget — never unconditionally (the bug),
     never zero (regressing #5949 stage ①-b's own accuracy need).

Real ``Session``/``RouterLoop``/``MediaStore`` throughout — most rows
here are produced via the SAME production write seam (``RouterLoop.
feedback`` -> ``persist_feedback``) #5896's/#5949's own acceptance tests
use; the MIGRATED-row tests below deliberately build a row missing
``CONTENT_BYTES_META_KEY`` by hand (lead-coder's BLOCKING review,
issuecomment-5578035547) — the production seam can never produce that
shape (it always stamps the field), so the migrated population (owner's
own real 597 MB history) needs its own construction to be observable
at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import MultimodalConfig
from reyn.config.chat import HistoryResidentConfig, LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, ResidentBytes
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"
# Comfortably over any of this file's small caps, comfortably under a real
# incident row (owner: hundreds of MB) — big enough to prove "not pulled
# in," small enough to keep the test fast.
_BODY = "z" * 20_000


def _mcp_env(content: str) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": content, "media_blocks": []}
    return {"status": "ok", "data": data, "_canonical_source": "mcp"}


def _round(content: str) -> ExecutionResult:
    return ExecutionResult(
        tool_results=[_mcp_env(content)],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )


def _session(agent_name: str, tmp_path: Path, *, max_bytes: int = 256 * 1024 * 1024, **kwargs):
    return make_session(
        agent_name=agent_name,
        multimodal_config=MultimodalConfig(),
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}.snap.json",
        safety=SafetyConfig(loop=LoopConfig(), on_limit=OnLimitConfig(mode="unattended")),
        history_resident_config=HistoryResidentConfig(max_bytes=ResidentBytes(max_bytes)),
        **kwargs,
    )


async def _write_one_content_ref_row(tmp_path: Path, agent_name: str, body: str) -> None:
    """Writes ONE real content_ref tool row via the production seam, then
    discards the session — only the durable history.jsonl + its
    history-content file matter to the tests below, which reload fresh
    with their OWN (often much smaller) ``max_bytes``."""
    session = _session(agent_name, tmp_path)
    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(_round(body))
    await loop.persist_feedback()


# ── ① eviction counts a content_ref row's real body, not its shell ──────


@pytest.mark.asyncio
async def test_eviction_counts_content_ref_body_bytes_not_the_serialized_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a resident content_ref row (~400-byte serialized shell)
    whose REAL body (20,000 bytes) alone exceeds the cap must be evicted
    on the next append — even though its own ``resident_bytes()`` alone
    would never trip a cap this small.

    Strip witness: reverting ``_evict_oldest_resident_entries``'s
    per-message size back to ``m.resident_bytes()`` alone (dropping the
    ``CONTENT_BYTES_META_KEY`` addition) keeps the content_ref row
    resident forever regardless of its real body size — verified
    directly, restored after."""
    from reyn.runtime.chat_message import ChatMessage

    monkeypatch.chdir(tmp_path)
    await _write_one_content_ref_row(tmp_path, "evict-agent", _BODY)

    # Cap sits ABOVE a lone small shell (~a few hundred bytes) but well
    # BELOW the content_ref row's real body (20,000 bytes) — the exact
    # gap #5973 traces: a cap that only ever saw the shell never fires.
    session = _session("evict-agent", tmp_path, max_bytes=4000)
    session.load_history()
    (content_ref_msg,) = [m for m in session.history if m.role == "tool"]
    assert content_ref_msg.meta.get(CONTENT_REF_META_KEY), (
        "sanity: this must genuinely be a content_ref row"
    )
    assert content_ref_msg.resident_bytes() < 1000, (
        "sanity: the row's own serialized shell must be tiny -- otherwise "
        "this test wouldn't distinguish 'counts the shell' from 'counts "
        "the real body'"
    )

    session._append_history(ChatMessage(role="user", content="one more turn"))

    assert content_ref_msg not in session.history, (
        "a content_ref row whose real body alone exceeds the cap must be "
        "evicted, even though its own resident shell never would trip it"
    )


# ── ② wire construction never materializes past its budget ──────────────


@pytest.mark.asyncio
async def test_build_history_sends_a_bounded_preview_when_over_the_wire_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ``build_history()`` must NOT materialize a content_ref
    row's real body when doing so would exceed the wire materialization
    budget (``history_resident.max_bytes``, #5973 裁定②) — it must still
    send SOMETHING for that turn (never silently drop it), just not the
    full body.

    Strip witness: removing the pre-resolve budget check in
    ``RouterHistoryBuffer._serialise_turn`` (falling through to an
    unconditional ``resolve_history_content`` call, the pre-#5973 shape)
    makes the wire carry the FULL body regardless of budget — verified
    directly, restored after."""
    monkeypatch.chdir(tmp_path)
    await _write_one_content_ref_row(tmp_path, "wire-budget-agent", _BODY)

    # Cap sits well under the row's real body (20,000 bytes) but well
    # over a bounded preview's own size.
    session = _session("wire-budget-agent", tmp_path, max_bytes=2000)
    session.load_history()

    wire = session._loop_driver._history_buffer.build_history()
    (wire_tool,) = [m for m in wire if m.get("role") == "tool"]

    assert _BODY not in wire_tool["content"], (
        "the full body must not reach the wire once the materialization "
        "budget this turn's own body alone exceeds"
    )
    assert wire_tool["content"], (
        "the turn must still carry SOMETHING (a bounded preview) — an "
        "over-budget turn is sent in spilled form, never silently dropped"
    )
    assert len(wire_tool["content"]) < len(_BODY), (
        "sanity: the substituted content must be materially smaller than "
        "the real body, or this isn't actually a bounded preview"
    )


@pytest.mark.asyncio
async def test_build_history_still_sends_the_full_body_under_a_generous_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the sibling non-regression assert (lead-coder's own
    standing requirement, #5949) — without this, "bounded the budget" and
    "broke the feature" are indistinguishable. The SAME row, under the
    default (generous) cap, still reaches the wire whole."""
    monkeypatch.chdir(tmp_path)
    await _write_one_content_ref_row(tmp_path, "wire-budget-ok-agent", _BODY)

    session = _session("wire-budget-ok-agent", tmp_path)  # default max_bytes
    session.load_history()

    wire = session._loop_driver._history_buffer.build_history()
    (wire_tool,) = [m for m in wire if m.get("role") == "tool"]

    assert _BODY in wire_tool["content"], (
        "under a generous budget the real body must still reach the wire "
        "— the bounded-preview substitution must be conditional on the "
        "budget, not unconditional"
    )


# ── ③ the recovery path hydrates candidates bounded, not all-or-nothing ─


@pytest.mark.asyncio
async def test_durable_active_history_after_hydrates_only_as_many_candidates_as_fit_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: compaction's own candidate read
    (``Session._durable_active_history_after``) must hydrate candidates
    UNTIL the shared materialization budget is spent, then stop — never
    unconditionally (the #5973 bug: a single recovery pass re-materialized
    an entire backlog), and never zero (would regress #5949 stage ①-b's
    own accuracy need for ``_measure_and_select``'s token estimates).

    Strip witness: reverting to unconditional hydration (``hydrate=True``
    at every candidate, the pre-#5973 shape) makes EVERY candidate's
    ``.content`` non-empty regardless of the budget — verified directly,
    restored after. Reverting the budget hydration to a no-op (#5949
    stage ①-b's own hydrate=False shape) makes EVERY candidate's
    ``.content`` stay empty instead — also verified directly."""
    monkeypatch.chdir(tmp_path)
    for i in range(3):
        await _write_one_content_ref_row(tmp_path, "recovery-budget-agent", _BODY)

    # Cap admits roughly ONE of the three ~20,000-byte bodies, not zero
    # and not all three.
    session = _session("recovery-budget-agent", tmp_path, max_bytes=25_000)
    session.load_history()

    candidates, _truncated = session._durable_active_history_after(0)
    tool_candidates = [m for m in candidates if m.role == "tool"]
    assert tool_candidates, "sanity: at least one row must be read as a candidate"
    assert all(m.meta.get(CONTENT_REF_META_KEY) for m in tool_candidates), (
        "sanity: every candidate returned must genuinely be a content_ref row"
    )

    hydrated = [m for m in tool_candidates if m.content != ""]
    empty = [m for m in tool_candidates if m.content == ""]
    assert hydrated, (
        "at least one candidate must be hydrated (real body materialized) "
        "— the budget must not zero out hydration entirely"
    )
    assert empty, (
        "at least one candidate must NOT be hydrated (its body left "
        "empty) — the budget must not hydrate every candidate "
        "unconditionally"
    )


# ── BLOCKING fix ①: a MIGRATED row (no meta["bytes"]) is still measured ──


async def _migrated_content_ref_row(tmp_path: Path, agent_name: str, body: str):
    """Builds a content_ref ``ChatMessage`` in the MIGRATED shape
    (``reyn storage migrate-bodies``, #5947:
    ``history_body_migration.py``'s own write stamps
    ``CONTENT_REF_META_KEY`` but NEVER ``CONTENT_BYTES_META_KEY``) — the
    production write seam (``RouterLoop.feedback`` -> ``persist_
    feedback``) can never produce this shape itself, so owner's own real
    population (an entirely-migrated 597 MB history) needs its own
    construction to be observable in a test at all.

    ``await store.flush()`` after the write — ``save_tool_result``'s own
    durable write goes through a background ``DurabilityWorker``
    (#5364 §1.4, "UIを止めさせたくない"); without the flush a read
    immediately after can race the write and see the file as not-yet-
    existing."""
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.chat_message import SPILLED_META_KEY, ChatMessage

    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path,
        agent_name=agent_name, session_id="s1",
    )
    ref_block = store.save_tool_result(body, tool="t", seq=1)
    await store.flush()
    return store, ChatMessage(
        role="tool", content="",
        meta={CONTENT_REF_META_KEY: ref_block["path"], SPILLED_META_KEY: False},
    )


@pytest.mark.asyncio
async def test_eviction_derives_body_bytes_via_stat_for_a_migrated_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5973 BLOCKING fix ① (lead-coder review,
    issuecomment-5578035547) — a MIGRATED content_ref row (no stamped
    ``CONTENT_BYTES_META_KEY``) must still be evicted once its REAL
    (stat-derived) body size exceeds the cap. Owner's own 597 MB history
    is entirely this population, not the freshly-written one every other
    test in this file drives — without this, ① passes every test here
    while doing nothing for the incident it was written for.

    Strip witness: reverting ``ChatMessage._derive_body_bytes`` to read
    ONLY ``CONTENT_BYTES_META_KEY`` (no stat fallback) keeps this row
    resident forever regardless of its real body size — verified
    directly, restored after."""
    from reyn.runtime.chat_message import ChatMessage

    monkeypatch.chdir(tmp_path)
    session = _session("migrated-evict-agent", tmp_path, max_bytes=4000)
    store, migrated_row = await _migrated_content_ref_row(tmp_path, "migrated-evict-agent", _BODY)
    session._media_store = store
    session._append_history(migrated_row)  # noqa: SLF001 - real durable-write seam

    assert migrated_row.meta.get("bytes") is None, (
        "sanity: this row must genuinely lack CONTENT_BYTES_META_KEY, or "
        "this test doesn't distinguish the migrated population from the "
        "freshly-written one"
    )

    session._append_history(ChatMessage(role="user", content="one more turn"))

    assert migrated_row not in session.history, (
        "a migrated content_ref row's real (stat-derived) body size must "
        "still evict it, even without a stamped CONTENT_BYTES_META_KEY"
    )


@pytest.mark.asyncio
async def test_build_history_derives_body_bytes_via_stat_for_a_migrated_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the ② sibling of the test above — ``build_history`` must
    still send a bounded preview (not the full body) for an over-budget
    MIGRATED row, even though its size is only knowable via a stat, never
    the meta field every other test's write seam stamps. A functional
    check, not an independent strip witness for the stat fallback itself
    (a row whose size is genuinely unknown is ALSO excluded from the
    materializable set here — same observable outcome either way; the
    eviction sibling test above is what actually distinguishes "correctly
    stat-derived" from "excluded because unknown")."""
    monkeypatch.chdir(tmp_path)
    session = _session("migrated-wire-agent", tmp_path, max_bytes=2000)
    store, migrated_row = await _migrated_content_ref_row(tmp_path, "migrated-wire-agent", _BODY)
    session._media_store = store
    session._append_history(migrated_row)  # noqa: SLF001 - real durable-write seam

    wire = session._loop_driver._history_buffer.build_history()
    (wire_tool,) = [m for m in wire if m.get("role") == "tool"]

    assert _BODY not in wire_tool["content"], (
        "a migrated row's full body must not reach the wire once its "
        "stat-derived size alone exceeds the budget"
    )


# ── BLOCKING fix ②: the wire budget protects the NEWEST turn ────────────


@pytest.mark.asyncio
async def test_build_history_budget_protects_the_newest_turn_not_the_oldest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5973 BLOCKING fix ② (lead-coder review,
    issuecomment-5578035547) — when the wire materialization budget can
    fit only ONE of several oversized content_ref rows, it must
    materialize the NEWEST one, not the oldest. ``turns`` serialises
    chronologically; a budget spent in that same order exhausts itself
    on old turns and leaves the newest — almost always the turn actually
    driving THIS send — with nothing left. #5973's own owner incident is
    a single ``hello`` send whose most recent tool result is exactly the
    huge one.

    Strip witness: reverting ``_select_materializable_refs`` to iterate
    ``turns`` chronologically (oldest-first) instead of ``reversed(turns)``
    makes the OLDEST row materialize instead of the newest — verified
    directly, restored after."""
    monkeypatch.chdir(tmp_path)
    agent_name = "newest-first-agent"
    await _write_one_content_ref_row(tmp_path, agent_name, "OLD" + _BODY)
    session = _session(agent_name, tmp_path)  # generous cap while writing
    from reyn.runtime.chat_message import ChatMessage
    session.load_history()
    session._append_history(ChatMessage(role="user", content="a plain turn in between"))  # noqa: SLF001

    loop = RouterLoop(host=session.router_host, chain_id="c2", router_model=_MODEL)
    loop.feedback(_round("NEW" + _BODY))
    await loop.persist_feedback()

    # Cap fits roughly ONE of the two ~20,000-byte bodies, not both.
    session2 = _session(agent_name, tmp_path, max_bytes=22_000)
    session2.load_history()

    wire = session2._loop_driver._history_buffer.build_history()
    wire_tools = [m for m in wire if m.get("role") == "tool"]
    assert wire_tools[1:], "sanity: both tool rows must reach the projection"

    assert "NEW" + _BODY in wire_tools[-1]["content"], (
        "the NEWEST tool result must be the one materialized in full "
        "when the budget cannot fit both"
    )
    assert "OLD" + _BODY not in wire_tools[0]["content"], (
        "the OLDEST tool result must be the one left as a bounded "
        "preview when the budget cannot fit both"
    )


# ── BLOCKING follow-up: the stat derivation is memoized ──────────────────


@pytest.mark.asyncio
async def test_body_bytes_derives_the_migrated_stat_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: #5973 BLOCKING follow-up (lead-coder review,
    issuecomment-5578156411) — ``ChatMessage.body_bytes()`` must derive a
    MIGRATED row's size via stat AT MOST ONCE per message, never once
    per call. ``Session._evict_oldest_resident_entries`` runs on EVERY
    append, scanning every resident row — an un-memoized stat here
    ``open()``s a migrated row's backing file once per resident migrated
    row PER APPEND (lead-coder's own measurement: the class this PR's
    own BLOCKING-1 fix introduced by deriving via stat without caching
    it).

    Strip witness: reverting ``body_bytes()`` to call
    ``_derive_body_bytes()`` unconditionally (dropping the cache) makes
    ``preview_calls`` grow with each call instead of staying at 1 —
    verified directly, restored after."""
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.chat_message import SPILLED_META_KEY, ChatMessage

    monkeypatch.chdir(tmp_path)

    class _CountingMediaStore(MediaStore):
        """A REAL ``MediaStore``, instrumented only to COUNT calls to
        ``read_tool_result_preview`` — the seam ``body_bytes()``'s cache
        exists to bound. Delegates fully to the real implementation;
        never fakes read behaviour."""

        def __init__(self, *a: object, **kw: object) -> None:
            super().__init__(*a, **kw)  # type: ignore[arg-type]
            self.preview_calls = 0

        def read_tool_result_preview(self, *a: object, **kw: object):  # type: ignore[override]
            self.preview_calls += 1
            return super().read_tool_result_preview(*a, **kw)  # type: ignore[arg-type]

    store = _CountingMediaStore(
        MediaStoreConfig(), project_root=tmp_path,
        agent_name="memoize-agent", session_id="s1",
    )
    ref_block = store.save_tool_result(_BODY, tool="t", seq=1)
    await store.flush()
    migrated_row = ChatMessage(
        role="tool", content="",
        meta={CONTENT_REF_META_KEY: ref_block["path"], SPILLED_META_KEY: False},
    )

    first = migrated_row.body_bytes(store)
    second = migrated_row.body_bytes(store)
    third = migrated_row.body_bytes(store)

    assert first == second == third == len(_BODY.encode("utf-8"))
    assert store.preview_calls == 1, (
        f"body_bytes() must derive the stat AT MOST ONCE per message, "
        f"got {store.preview_calls} calls across 3 reads"
    )


# ── BLOCKING follow-up 2: an out-of-boundary ref degrades, never raises ──


@pytest.mark.asyncio
async def test_body_bytes_returns_none_for_an_out_of_boundary_ref_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5973 BLOCKING follow-up (lead-coder review,
    issuecomment-5578223027) — a content_ref row whose ref names a path
    OUTSIDE ``media_store``'s own boundary must fold to ``None`` (the
    same "unknown" every other branch returns), never raise. ``body_
    bytes()`` runs on the hot append path (``Session._evict_oldest_
    resident_entries``, called from EVERY ``Session._append_history``)
    — an uncaught ``PermissionError`` for one such row would make every
    future append fail, turning a degrade into a hard stop.

    Strip witness: removing the ``try/except PermissionError`` around
    the stat call makes this test itself raise instead of asserting —
    verified directly, restored after."""
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.chat_message import SPILLED_META_KEY, ChatMessage

    monkeypatch.chdir(tmp_path)
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path,
        agent_name="boundary-agent", session_id="s1",
    )
    outside_row = ChatMessage(
        role="tool", content="",
        meta={
            CONTENT_REF_META_KEY: "../outside-the-project.txt",
            SPILLED_META_KEY: False,
        },
    )

    result = outside_row.body_bytes(store)

    assert result is None, (
        "an out-of-boundary ref must derive to None (unknown), not raise "
        "and not report a fabricated size"
    )


@pytest.mark.asyncio
async def test_append_survives_an_out_of_boundary_content_ref_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the ① sibling of the test above, driven through the REAL
    hot path — an out-of-boundary content_ref row resident in
    ``self.history`` must not make ``Session._append_history`` (which
    runs ``_evict_oldest_resident_entries`` on every call) raise."""
    from reyn.runtime.chat_message import ChatMessage

    monkeypatch.chdir(tmp_path)
    session = _session("boundary-append-agent", tmp_path, max_bytes=4000)
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    session._media_store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path,
        agent_name="boundary-append-agent", session_id="s1",
    )
    outside_row = ChatMessage(
        role="tool", content="",
        meta={
            CONTENT_REF_META_KEY: "../outside-the-project.txt",
            "spilled": False,
        },
    )
    session._append_history(outside_row)  # noqa: SLF001 - real durable-write seam

    # The real assertion is that this does not raise; a second append
    # (re-running the eviction scan the boundary-violating row survives
    # in) is the strongest witness that it keeps not raising.
    session._append_history(ChatMessage(role="user", content="one more turn"))  # noqa: SLF001
