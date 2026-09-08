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

Real ``Session``/``RouterLoop``/``MediaStore`` throughout — every
content_ref row here is produced via the SAME production write seam
(``RouterLoop.feedback`` -> ``persist_feedback``) #5896's/#5949's own
acceptance tests use, never a hand-built row.
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
