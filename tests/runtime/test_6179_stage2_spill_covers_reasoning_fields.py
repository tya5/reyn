"""Tier 2: #6179 stage ⑵ — the reactive spill mechanism (``_spill_batch_
within_face``, ``router_loop_driver.py``) now also covers a turn's own
reasoning-bundle fields (#1652: ``reasoning_content`` / ``thinking_blocks`` /
``provider_specific_fields``), not ``content`` alone.

Root defect (architect census, #6179): spill's own unit was ``content`` — a
single string per turn (``spill_turn_content(content: str)``,
``is_already_spilled(turn["content"])``, and the replacement's own
``{**turn, "content": replacement}`` shallow copy). Reasoning fields reach
the wire (``RouterHistoryBuffer._serialise_turn``, #1652 ②) but spill's own
candidate check and replacement never looked at them — a genuinely huge
``reasoning_content`` sat untouched no matter how eligible its own
``Spillability`` declaration was.

Owner ruling (2026-09-14, verbatim): "すべておくるべきだし、spill 対象にする
のが自然だと思うな" — reasoning becomes subject to the SAME shrink-flow
bound as everything else, rather than the separate, uncoordinated
``chat.reasoning.recent_turns`` bound. This PR is stage ⑵ ONLY (spill
covers reasoning) — stage ⑴ (``chat.reasoning.recent_turns: 0``, config
only) is explicitly deferred to AFTER this lands (lead-coder ruling: the
`_bound_wire_reasoning` pop is the ONLY subject bounding reasoning's own
size today; removing it before spill covers reasoning would leave nobody
bounding it at all, CLAUDE.md's own Q1).

Real ``RouterLoopDriver`` + real ``RouterHistoryBuffer`` + real
``MediaStore`` (mirrors ``test_5720_spill_carries_real_turn_provenance.py``'s
own established recipe for this exact triple) — no mocks.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.budget_gateway import BudgetGateway
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.runtime.services.router_loop_driver import RouterLoopDriver


class _FakeRouterHost:
    """Minimal stand-in — ``_spill_batch_for_retry``'s own code path never
    reads it (same tolerance ``test_5720_spill_carries_real_turn_
    provenance.py``'s own real-driver test already establishes)."""

    def _set_cancel_event(self, event):
        pass


class _FakeBudget(BudgetGateway):
    """Inherits the real ``BudgetGateway`` (cheap to construct) rather than
    hand-reimplementing it — the SAME shape #5748 established, so a future
    method this test doesn't drive is the real one by construction."""

    def __init__(self) -> None:
        super().__init__(budget_tracker=None, events=EventLog(), agent_name="t-agent")

    def check_and_increment_router_cap(self, user_text):
        pass

    def extend_router_cap(self, additional):
        pass

    def add_router_usage(self, **kwargs):
        pass


class _FakeBudgetAdvisor:
    async def enforce_new_msg_budget(self, **kwargs):
        pass


async def _noop_limit_checkpoint(**kwargs):
    from types import SimpleNamespace
    return SimpleNamespace(allow_continue=True, extension=1)


def _make_real_driver(tmp_path, *, history: "list"):
    from reyn.config.chat import SafetyConfig
    from reyn.data.workspace.media_store import MediaStore
    store = MediaStore(
        project_root=tmp_path, agent_name="t-agent", session_id="t-session",
    )
    events = EventLog()
    compaction_cfg = CompactionConfig(use_chars4_estimate=True)
    history_buffer = RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=compaction_cfg,
        compaction_controller=None,
        model_fn=lambda: "test-model",
        events=events,
        media_store=store,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=True,
        history_appender=history.append,
    )
    driver = RouterLoopDriver(
        router_host=_FakeRouterHost(),
        safety=SafetyConfig(),
        router_max_iterations=1,
        budget_tracker=_FakeBudget(),
        non_interactive=True,
        exclude_tools=set(),
        budget=_FakeBudget(),
        resolver=None,
        compaction=compaction_cfg,
        compaction_controller=None,
        token_learner=None,
        events=events,
        model_override_fn=lambda: None,
        history_buffer=history_buffer,
        budget_advisor=_FakeBudgetAdvisor(),
        limit_checkpoint_fn=_noop_limit_checkpoint,
        next_seq_fn=lambda: 0,
        append_history_fn=history.append,
    )
    return driver, history_buffer


# ---------------------------------------------------------------------------
# B — `_REASONING_BUNDLE_FIELDS` is now DERIVED from
# `_REASONING_BUNDLE_SPILLABLE_FIELDS` (`_REASONING_BUNDLE_FIELDS = tuple(
# _REASONING_BUNDLE_SPILLABLE_FIELDS)`, reasoning_continuity.py) — a set-
# equality test between the two would be vacuously true by construction
# (there is nothing left for it to catch; the two CANNOT diverge, not
# "usually don't"), so no such test exists here (six questions Q4 — a green
# with nothing to bite on). The real accept-side witnesses for B are the
# behavioral tests below (thinking_blocks is never spilled; reasoning_
# content is), which exercise the mapping's own VALUES, not its key set.
# ---------------------------------------------------------------------------
# Accept item 1 — thinking_blocks is NEVER replaced; reasoning_content on
# the SAME turn IS (positive control — "nothing happened" must not read as
# green).
# ---------------------------------------------------------------------------


def test_thinking_blocks_never_replaced_reasoning_content_on_same_turn_is(
    tmp_path,
):
    """Tier 2: #6179 stage ⑵ accept item 1 — a turn carrying BOTH
    ``reasoning_content`` (spillable) and ``thinking_blocks`` (structurally
    NOT spillable — Anthropic/DeepSeek DIRECT-API require the native
    round-trip on it) is offered as a spill candidate: ``reasoning_content``
    is replaced with a preview, ``thinking_blocks`` is untouched, byte-for-
    byte the same object it started as.

    Strip-falsifier: removing the ``if not _spillable: continue`` guard in
    ``_spill_batch_within_face`` (or flipping ``thinking_blocks``'s own
    mapping entry to ``True``) would make this test's own
    ``thinking_blocks`` assertion fail — it would be replaced with a
    preview STRING instead of staying the original list."""
    history: "list" = []
    driver, history_buffer = _make_real_driver(tmp_path, history=history)

    thinking_blocks_original = [{"type": "thinking", "text": "must never be touched"}]
    turn = {
        "role": "assistant",
        "content": "large tool-adjacent content " * 200,
        "reasoning_content": "large reasoning text " * 200,
        "thinking_blocks": thinking_blocks_original,
        "spillability": "first_choice",
    }

    # #6240 ④: _spill_batch_for_retry now requires record_sink — this
    # test doesn't assert on it (no is_already_spilled check follows in
    # THIS test), a throwaway list is enough.
    edits = driver._spill_batch_for_retry(
        [turn], chain_id="c1", seq_by_id={id(turn): 1}, record_sink=[],
    )

    (only,) = edits  # exactly one candidate, exactly one edit
    _idx, replaced_turn = only
    assert replaced_turn["reasoning_content"] != turn["reasoning_content"], (
        "reasoning_content must be replaced with a preview"
    )
    assert isinstance(replaced_turn["reasoning_content"], str)
    assert replaced_turn["thinking_blocks"] is thinking_blocks_original, (
        "thinking_blocks must be untouched -- the SAME object, never "
        "replaced with a preview string"
    )


def test_not_spillable_mapping_protects_a_field_even_if_it_were_a_string(
    tmp_path,
):
    """Tier 2: #6179 stage ⑵ ("B") — isolates the MAPPING's own guard
    (``_REASONING_BUNDLE_SPILLABLE_FIELDS[field] is False``) from the
    incidental ``isinstance(value, str)`` type check the loop also applies.
    ``thinking_blocks`` is realistically a list/dict on the real wire (see
    the sibling test above), so that type check alone would ALSO protect
    it there — this test uses a synthetic STRING value under the
    ``thinking_blocks`` key specifically to prove the MAPPING's own "False"
    entry is what stops it, independent of the value's shape.

    Strip-falsifier (performed during review): flipping
    ``_REASONING_BUNDLE_SPILLABLE_FIELDS["thinking_blocks"]`` to ``True``
    makes this test go RED — with a string value, the type check no
    longer protects it, so only the mapping's own "False" entry does."""
    history: "list" = []
    driver, history_buffer = _make_real_driver(tmp_path, history=history)

    turn = {
        "role": "assistant",
        "content": "large content " * 200,
        # Synthetic: a string value under a key the mapping marks
        # unspillable, to isolate the mapping's own guard from the
        # isinstance(str) check the realistic (list) shape would also
        # satisfy.
        "thinking_blocks": "a string value that must still never be spilled " * 50,
        "spillability": "first_choice",
    }

    # #6240 ④: _spill_batch_for_retry now requires record_sink — this
    # test doesn't assert on it (no is_already_spilled check follows in
    # THIS test), a throwaway list is enough.
    edits = driver._spill_batch_for_retry(
        [turn], chain_id="c1", seq_by_id={id(turn): 1}, record_sink=[],
    )

    (only,) = edits  # content alone still makes progress
    _idx, replaced_turn = only
    assert replaced_turn["thinking_blocks"] == turn["thinking_blocks"], (
        "thinking_blocks must stay untouched even when it happens to be "
        "a string -- the mapping's own False entry protects it, not the "
        "value's shape"
    )


# ---------------------------------------------------------------------------
# Accept item 2 — the progress witness: a turn whose content is ALREADY
# spilled but whose reasoning is NOT yet spilled must still make progress
# (reasoning gets spilled), and must NOT be re-offered as a source of
# further progress once both are done.
# ---------------------------------------------------------------------------


def test_content_already_spilled_turn_still_spills_its_reasoning_field(
    tmp_path,
):
    """Tier 2: #6179 stage ⑵ accept item 2, part 1 (the load-bearing fix,
    "C") — before this PR, ``is_already_spilled(turn["content"])`` alone
    gated the WHOLE turn (``if is_already_spilled(...): continue``): a
    turn whose content was already a spilled preview was skipped in its
    entirety, and its own (never-yet-spilled) ``reasoning_content`` was
    never even looked at -- permanently, since content's own "already
    spilled" state never changes back. This is the "mirror image" of the
    infinite loop ``is_already_spilled``'s own docstring names: real
    progress was available and never taken, so ADR-0049 §1's own ④
    (unspilled candidate count) never strictly decreased for this turn.

    Real ``spill_turn_content`` produces the "already spilled" content
    (not a hand-typed string) -- the SAME real preview mechanism the
    positive path uses, driven directly rather than assumed."""
    history: "list" = []
    driver, history_buffer = _make_real_driver(tmp_path, history=history)

    # Produce a REAL spilled-preview content string via the same mechanism
    # under test, so "already spilled" is verified, not asserted.
    # #6240 ④: spill_turn_content returns its durable record now rather
    # than appending it itself — appended here directly (this file's
    # own `history_appender=history.append` wiring) so
    # `is_already_spilled` (reading the supersede map back off
    # `history_fn()`) recognises it below, matching production.
    _outcome0 = history_buffer.spill_turn_content(
        "original oversized content " * 200, chain_id="c0", tool="history", seq=1,
    )
    already_spilled_content = _outcome0.replacement
    assert already_spilled_content is not None
    assert _outcome0.record is not None
    history.append(_outcome0.record)
    assert history_buffer.is_already_spilled(already_spilled_content)

    turn = {
        "role": "assistant",
        "content": already_spilled_content,
        "reasoning_content": "large, never-yet-spilled reasoning text " * 200,
        "spillability": "first_choice",
    }

    # #6240 ④: _spill_batch_for_retry now requires record_sink — this
    # test doesn't assert on it (no is_already_spilled check follows in
    # THIS test), a throwaway list is enough.
    edits = driver._spill_batch_for_retry(
        [turn], chain_id="c1", seq_by_id={id(turn): 1}, record_sink=[],
    )

    (only,) = edits  # progress: exactly one edit, not an empty batch
    _idx, replaced_turn = only
    assert replaced_turn["content"] == already_spilled_content, (
        "content must stay untouched -- it was already spilled"
    )
    assert replaced_turn["reasoning_content"] != turn["reasoning_content"], (
        "reasoning_content must be spilled even though content alone "
        "would have read as 'already spilled, nothing to do'"
    )


def test_fully_spilled_turn_is_not_re_offered_as_further_progress(tmp_path):
    """Tier 2: #6179 stage ⑵ accept item 2, part 2 -- the OTHER half of
    the same witness. Once BOTH a turn's content and its reasoning field
    are already-spilled previews, a SECOND pass over the SAME turn
    produces NO edit (an empty batch) -- it is not endlessly re-offered as
    if there were still progress to make."""
    history: "list" = []
    driver, history_buffer = _make_real_driver(tmp_path, history=history)

    # #6240 ④: same as the sibling test above — append each returned
    # record directly, playing the production loop-side appender role.
    _outcome_c = history_buffer.spill_turn_content(
        "original oversized content " * 200, chain_id="c0", tool="history", seq=1,
    )
    _outcome_r = history_buffer.spill_turn_content(
        "original oversized reasoning " * 200, chain_id="c0", tool="history", seq=2,
    )
    spilled_content = _outcome_c.replacement
    spilled_reasoning = _outcome_r.replacement
    assert spilled_content is not None
    assert spilled_reasoning is not None
    assert _outcome_c.record is not None and _outcome_r.record is not None
    history.append(_outcome_c.record)
    history.append(_outcome_r.record)

    fully_spilled_turn = {
        "role": "assistant",
        "content": spilled_content,
        "reasoning_content": spilled_reasoning,
        "spillability": "first_choice",
    }

    edits = driver._spill_batch_for_retry(
        [fully_spilled_turn], chain_id="c1", seq_by_id={id(fully_spilled_turn): 1},
        record_sink=[],
    )

    assert edits == [], (
        "a turn whose content AND reasoning are both already-spilled "
        "previews must not be re-offered as a source of further progress"
    )
