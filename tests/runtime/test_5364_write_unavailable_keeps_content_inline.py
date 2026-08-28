"""Tier 2: #5364 §1.5 — the failure path (A's exception). Owner: "a
permanently-failed write's turn keeps content inline, never emits a ref
naming a file that doesn't exist." ``MediaStoreWriteUnavailable`` is the
typed signal: ``save_tool_result`` raises it once
``DurabilityWorker.durability_failed`` is known True (an EARLIER write
already exhausted §4 retries) rather than minting a ref for a write that
will never land. ``cap_tool_result_content`` catches it and keeps
``content_str`` unchanged; ``RouterLoop.feedback`` stamps
``LOST_REASON_META_KEY=LOST_REASON_NEVER_PERSISTED`` on the entry so an
operator sees WHY, without any ref ever having been claimed.

Three tests, three collaborators, narrowest-to-widest:

- :func:`test_save_tool_result_refuses_once_durability_has_permanently_failed`
  — real ``MediaStore`` + real ``DurabilityWorker`` (fast retry bounds,
  no mocks) — a genuinely exhausted write latches ``durability_failed``,
  the NEXT ``save_tool_result`` call raises immediately (no attempt).
- :func:`test_cap_tool_result_content_keeps_content_inline_on_write_unavailable`
  — a real (raising) callable stands in for ``save_fn`` — proves the
  catch + inline fallback + ``on_write_unavailable`` firing.
- :func:`test_the_real_production_chain_marks_the_entry_never_persisted`
  — a real ``Session`` driven end-to-end through ``session.router_host``
  (mirrors #5372's own real-chain test) — proves the wiring survives all
  4 hops, not just a fast unit-level stand-in.
"""
from __future__ import annotations

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.config.chat import OffloadConfig
from reyn.core.events.durability_worker import DurabilityWorker
from reyn.core.events.events import EventLog
from reyn.data.workspace.media_store import MediaStore, MediaStoreWriteUnavailable
from reyn.runtime.chat_message import (
    CONTENT_REF_META_KEY,
    LOST_REASON_META_KEY,
    LOST_REASON_NEVER_PERSISTED,
    SPILLED_META_KEY,
)
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import TRIGGER_CAP, cap_tool_result_content
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger


def _fast_worker(max_attempts: int = 2) -> DurabilityWorker:
    return DurabilityWorker(
        max_write_attempts=max_attempts, retry_base_s=0.001, retry_max_s=0.005,
    )


@pytest.mark.asyncio
async def test_save_tool_result_refuses_once_durability_has_permanently_failed(
    tmp_path,
) -> None:
    """Tier 2: an EARLIER write's persistent failure (real worker, real
    exhausted retries, no filesystem trickery needed) latches
    ``durability_failed`` — the NEXT ``save_tool_result`` call raises
    ``MediaStoreWriteUnavailable`` immediately, never attempting a write
    that is now known to be pointless. The store's own store_dir stays a
    perfectly ordinary writable directory throughout — proves the refusal
    is driven by the latched flag, not by anything actually broken on
    disk."""
    worker = _fast_worker()
    store = MediaStore(project_root=tmp_path, session_id="broken", worker=worker)
    assert store.durability_failed is False, "test setup sanity: starts healthy"

    async def _always_fail() -> None:
        raise OSError("disk full")

    worker.submit_nowait(_always_fail)
    await worker.flush()
    assert store.durability_failed is True, (
        "test setup sanity: the seeded failure must have latched durability_failed"
    )

    with pytest.raises(MediaStoreWriteUnavailable):
        store.save_tool_result("this must never actually be attempted")


def test_cap_tool_result_content_keeps_content_inline_on_write_unavailable() -> None:
    """Tier 2: ``cap_tool_result_content`` catches ``MediaStoreWriteUnavailable``
    from ``save_fn`` and returns ``content_str`` UNCHANGED (never a
    preview naming a ref that will never exist) — the same shape as the
    "content stayed under the cap" no-op path. ``on_write_unavailable``
    fires exactly once; a real events sink also gets the audit event."""
    calls: list[None] = []

    def _always_unavailable(*_a, **_kw):
        raise MediaStoreWriteUnavailable("store is broken")

    events = EventLog(subscribers=[])
    emitted: list[dict] = []
    events.add_subscriber(lambda e: emitted.append({"type": e.type, **e.data}))
    big = "X" * 100_000

    result = cap_tool_result_content(
        big, cap_tokens=10, model=_MODEL, save_fn=_always_unavailable,
        trigger=TRIGGER_CAP, use_chars4=True, events=events,
        on_write_unavailable=lambda: calls.append(None),
    )

    assert result == big, "content must stay inline, unchanged — no ref was ever minted"
    assert calls == [None], "on_write_unavailable must fire exactly once"
    kinds = [e["type"] for e in emitted]
    assert "tool_result_write_unavailable" in kinds, (
        f"expected a tool_result_write_unavailable audit event, got: {kinds!r}"
    )
    assert "tool_result_offloaded" not in kinds, (
        "no offload happened — the offload event must not fire"
    )


def test_the_real_production_chain_marks_the_entry_never_persisted(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: architect/lead-coder's own #5372 precedent — a real
    ``Session``, real ``RouterHostAdapter`` (``session.router_host``),
    real ``ContextBudgetAdvisor``, driven end-to-end. The store's worker
    is swapped for a fast one and seeded with one genuinely-exhausted
    failure (same technique as the first test above) BEFORE the turn
    runs, so the write-time cap's own attempt hits the pre-check and
    raises — proving the full 4-hop ``on_write_unavailable`` wiring, not
    just a fast unit-level stand-in."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="broken-store-agent",
        multimodal_config=MultimodalConfig(),
        offload_config=OffloadConfig(enabled=True),
        compaction_config=CompactionConfig(use_chars4_estimate=True),
    )
    worker = _fast_worker()
    session.router_host.media_store._worker = worker

    async def _always_fail() -> None:
        raise OSError("disk full")

    async def _seed_failure() -> None:
        worker.submit_nowait(_always_fail)
        await worker.flush()

    import asyncio
    asyncio.run(_seed_failure())
    assert session.router_host.media_store.durability_failed is True, (
        "test setup sanity: the seeded failure must have latched durability_failed"
    )

    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[{
            "status": "ok",
            "data": {
                "kind": "mcp", "status": "ok", "server": "s", "tool": "t",
                "content": _BIG, "media_blocks": [],
            },
            "_canonical_source": "mcp",
        }],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    loop.feedback(result)

    (tool_msg,) = [m for m in session.history if m.role == "tool"]
    assert SPILLED_META_KEY not in tool_msg.meta, (
        "no ref was ever minted — SPILLED_META_KEY must stay absent"
    )
    assert CONTENT_REF_META_KEY not in tool_msg.meta
    assert tool_msg.meta.get(LOST_REASON_META_KEY) == LOST_REASON_NEVER_PERSISTED, (
        f"expected LOST_REASON_NEVER_PERSISTED, got meta={tool_msg.meta!r}"
    )
    assert _BIG in tool_msg.content, (
        "the ORIGINAL content must still be present in the rendered turn "
        "(inline, never truncated to a dead ref)"
    )
