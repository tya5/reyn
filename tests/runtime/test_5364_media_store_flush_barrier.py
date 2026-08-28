"""Tier 2: #5364 §1.4 — ``MediaStore.save_tool_result``'s actual disk write
moves OFF the event loop (owner: "UIを止めさせたくない" — a synchronous write
onto slow/network storage stalls the whole loop, the same class of problem
#1765 fixed for the WAL). ``DurabilityWorker.submit_nowait`` enqueues it
fire-and-forget; the ONE barrier that makes the write observable again is
``router_loop.py``'s own ``run_loop`` — once per iteration, right before the
LLM sees this turn's history, never per-write (a per-write flush would
reintroduce the very stall this section moved the write off the loop to
avoid).

Two tests, two collaborators:

- :func:`test_save_tool_result_write_is_deferred_until_flush` drives
  ``MediaStore`` + a real ``DurabilityWorker`` directly (no RouterLoop
  involved) — proves the deferral + the barrier's OWN contract.
- :func:`test_router_loop_flushes_pending_writes_before_the_llm_call` drives
  the ACTUAL ``router_loop.py`` call site — proves the barrier fires at the
  right point in the real control flow, not just that ``MediaStore.flush``
  works in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.data.workspace.media_store import MediaStore
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import TRIGGER_CAP, cap_tool_result_content
from reyn.tools.scheme import ExecutionResult
from tests._support.router_loop import FakeRouterHost

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger


@pytest.mark.asyncio
async def test_save_tool_result_write_is_deferred_until_flush(tmp_path: Path) -> None:
    """Tier 2: inside a running loop, ``save_tool_result``'s block/hash/path
    are all synchronous (the caller gets a usable ref immediately), but the
    actual bytes on disk are NOT there yet until :meth:`MediaStore.flush`
    is awaited — the write ran off-loop, fire-and-forget."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="flush-test")

    block = store.save_tool_result(_BIG)
    written = tmp_path / block["path"]

    # No await has happened yet since save_tool_result returned (still the
    # same synchronous call stack) — the drainer task cannot have run.
    assert not written.exists(), (
        "the write must not be on disk yet — save_tool_result only "
        "ENQUEUES it (submit_nowait), it must not have run synchronously"
    )

    await store.flush()

    assert written.is_file(), "flush() must make the enqueued write durable"
    assert written.read_text(encoding="utf-8") == _BIG


@pytest.mark.asyncio
async def test_flush_orders_the_manifest_append_after_the_content_write(
    tmp_path: Path,
) -> None:
    """Tier 2: architect (#5384②) — a witness for the CONTENT-write-then-
    manifest-append ORDER itself (save_tool_result's own comment on why
    the manifest is submitted after the content job, same worker,
    FIFO-serial) — not just "both eventually land" (a fresh read taken
    AFTER flush cannot distinguish the two orderings: flush() drains the
    content job either way, so by then the fresh reader sees the same
    thing regardless of which write actually ran first — confirmed by
    strip-falsify: an EARLIER attempt at this test that read only after
    flush stayed green under a reordered strip).

    The real distinguishing moment is a fresh instance reading the
    manifest DURING the still-pending window, BEFORE flush. Correct
    ordering (manifest deferred behind content, same worker) means
    nothing is on disk yet at that moment — an ordinary, harmless miss,
    self-prune has nothing to corrupt. The bug this guards (manifest
    written eagerly, ahead of content) means the line WOULD already be
    on disk at that moment, pointing at a not-yet-existing file — that
    fresh read's own construction-time self-prune (``_load_spill_manifest``'s
    own docstring) would DELETE it right then, permanently — so a LATER,
    properly-flushed reader would ALSO miss it forever, not just once."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="flush-test")

    block = store.save_tool_result(_BIG)
    # No await yet — still inside the pending window. A fresh reader here
    # exercises self-prune against whatever is (or isn't) on disk RIGHT
    # NOW, before this store's own flush() has made anything durable.
    early_reader = MediaStore(
        project_root=tmp_path, agent_name="test-agent", session_id="flush-test",
    )
    early_reader.is_tool_result_spill(block["path"])  # side effect: may self-prune

    await store.flush()

    late_reader = MediaStore(
        project_root=tmp_path, agent_name="test-agent", session_id="flush-test",
    )
    assert late_reader.is_tool_result_spill(block["path"]), (
        "a reader constructed WHILE the write was still pending "
        "permanently pruned the manifest entry — the manifest line was "
        "on disk before the content file it names, the exact ordering "
        "bug §1.4 fixed"
    )


class _MediaStoreHost(FakeRouterHost):
    """FakeRouterHost + a REAL MediaStore wired through ``cap_tool_result``
    (mirrors ``_RecordingHost``/``_CapHost``'s established shape in the
    sibling test files) — this test's target is ``router_loop.py``'s OWN
    flush call site, not the on_offload forwarding chain (#5372 already
    covers that with the real production adapter chain), so a FakeRouterHost
    is the right-weight collaborator here: cheap, deterministic, and CLAUDE.md
    only forbids faking what's cheaply constructible — MediaStore itself
    stays real."""

    def __init__(self, store: MediaStore) -> None:
        super().__init__()
        self.media_store = store

    def cap_tool_result(
        self, content_str: str, *, content_type: "str | None" = None, on_offload=None,
        on_write_unavailable=None,
    ) -> str:
        return cap_tool_result_content(
            content_str, cap_tokens=100, model=_MODEL,
            save_fn=self.media_store.save_tool_result, trigger=TRIGGER_CAP,
            use_chars4=True, content_type=content_type, on_offload=on_offload,
            on_write_unavailable=on_write_unavailable,
        )

    def media_followup_budget(self, _content_str: str) -> int:
        return 500


class _ExistenceCheckingLLM:
    """Real callable (not a mock) replacing ``call_llm_tools`` — records
    whether ``target`` existed on disk at the moment it was invoked, then
    ends the turn with a plain text reply (no tool_calls, so the loop stops
    after this one call)."""

    def __init__(self, target: Path) -> None:
        self._target = target
        self.existed_when_called: "bool | None" = None
        self.call_count = 0

    async def __call__(self, **kwargs) -> LLMToolCallResult:
        self.call_count += 1
        self.existed_when_called = self._target.is_file()
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )


async def _seed_pending_spill(host: _MediaStoreHost) -> Path:
    """Enqueue one spilled write via the SAME chokepoint a real chat turn
    uses (``RouterLoop.feedback``), synchronously — no ``await`` happens
    between this call and the caller's next statement, so the write is
    still PENDING (the drainer task has had no chance to run) by the time
    ``run_loop`` is entered."""
    loop = RouterLoop(host=host, chain_id="c-seed", router_model=_MODEL)
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": _BIG, "media_blocks": []}
    env = {"status": "ok", "data": data, "_canonical_source": "mcp"}
    result = ExecutionResult(
        tool_results=[env],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    loop.feedback(result)
    (entry,) = [e for e in host.history if e.get("role") == "tool"]
    ref = entry["meta"]["content_ref"]
    return host.media_store._project_root / ref


@pytest.mark.asyncio
async def test_router_loop_flushes_pending_writes_before_the_llm_call(tmp_path: Path) -> None:
    """Tier 2: #5364 §1.4 — ``run_loop``'s own flush call site (right before
    the LLM sees this turn), driven for real: a write enqueued BEFORE
    ``run_loop`` starts is durable by the time the (real, injected)
    ``_llm_caller`` is invoked for this turn's very first iteration.

    No await happens anywhere in ``run_loop`` between the top of its
    per-iteration loop and its flush call site for a bare
    ``FakeRouterHost`` (no ``should_force_close`` /
    ``peek_mid_turn_injection`` implemented — both getattr-guarded, both
    short-circuit BEFORE their own ``await``, never reaching it). This is
    an invariant about the CODE PATH (guard-then-await, in that order,
    both gated on a host attribute this fixture never implements), not a
    line-number range — confirmed by reading ``router_loop.py``'s
    ``run_loop`` directly at the head of each iteration, before the
    flush call. If a future edit adds a new unconditional ``await``
    ahead of the flush, this test would go green on scheduler luck
    rather than the barrier — re-verify this comment's claim (not just
    that the test still passes) whenever that region changes. So this is
    a genuine ordering witness, not a scheduler-timing coincidence:
    nothing else in this call graph could have run the drainer first."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="flush-test")
    host = _MediaStoreHost(store)
    written = await _seed_pending_spill(host)
    assert not written.exists(), (
        "test setup sanity: the seeded write must still be pending "
        "(no await occurred between feedback() and here)"
    )

    llm = _ExistenceCheckingLLM(written)
    loop = RouterLoop(host=host, chain_id="c-turn", llm_caller=llm, max_iterations=3)
    await loop.run_loop(messages=[{"role": "user", "content": "go"}], tools=[], _univ_enabled=False)

    assert llm.call_count == 1, "test setup sanity: the LLM must have been called exactly once"
    assert llm.existed_when_called is True, (
        "the pending write was NOT durable yet when the LLM was called — "
        "run_loop's flush barrier did not run before this turn's LLM call"
    )
