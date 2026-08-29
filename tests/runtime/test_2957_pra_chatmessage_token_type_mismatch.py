"""Tier 2: #2957 PR-A — ChatMessage/dict type-mismatch token under-count fix.

``estimate_tokens_for_turn`` (``reyn.services.compaction.engine``) is dict-only
by design: ``turn.get("content")`` raises ``AttributeError`` on nothing (a
plain dataclass has no ``.get``), so ``isinstance(turn, dict)`` silently gates
it to False for a ``ChatMessage`` and the function always fell through to its
``content is None`` fallback branch — counting only ``str(turn.text)`` (the
first text part). Real callers (``decompose_history_for_retry``, and
``trim_head``/``trim_tail`` via ``_trim_groups``) pass live ``ChatMessage``
instances, so:

  - image content parts never hit ``_IMAGE_FIXED_TOKEN_COST`` (under-counted
    against the real per-item token cost), and
  - ``tool_calls`` were never counted at all.

#5367: ``RouterHistoryBuffer.build_history`` itself was ALSO a real caller
of this fix when this file was written — it used the estimate for its own
elide/no-elide decision (#1128 Fork B). That decision (and the whole
elide branch) is retired (owner ruling, #5367); the integration witness
this file used to carry for it (``test_build_history_elides_when_image_
cost_pushes_over_trigger``) is removed in the same PR — the two REMAINING
real callers above still exercise this fix directly.

``estimate_tokens_for_any_turn`` is the new thin call-site adapter (NOT a
change to ``estimate_tokens_for_turn`` itself — that function stays dict-only
per the #2957 PR-A design gate) that converts a live ``ChatMessage`` into the
dict shape the estimator already knows how to walk, without running
``_serialise_turn``'s path-ref -> base64 materialisation.

Policy compliance:
- No unittest.mock / fakes — real ChatMessage, real
  engine functions throughout.
- No private-state assertions.
- Each docstring opens with ``Tier 2: ``.
"""
from __future__ import annotations

from reyn.runtime.chat_message import ChatMessage
from reyn.services.compaction.engine import (
    _IMAGE_FIXED_TOKEN_COST,
    estimate_tokens_for_any_turn,
    estimate_tokens_for_turn,
    trim_head,
    trim_tail,
)

# ---------------------------------------------------------------------------
# estimate_tokens_for_any_turn — dict passthrough (regression pin)
# ---------------------------------------------------------------------------


def test_dict_turn_passthrough_unchanged() -> None:
    """Tier 2: a plain dict turn is delegated to estimate_tokens_for_turn
    UNCHANGED — the adapter must not alter the existing dict-shape callers
    (retry_loop, context_budget_advisor) that already pass wire dicts."""
    turn = {"role": "user", "content": "a" * 400}
    direct = estimate_tokens_for_turn(turn, model="", use_chars4=True)
    via_adapter = estimate_tokens_for_any_turn(turn, model="", use_chars4=True)
    assert via_adapter == direct == 100


# ---------------------------------------------------------------------------
# estimate_tokens_for_any_turn — text-only ChatMessage (regression pin)
# ---------------------------------------------------------------------------


def test_text_only_chat_message_unchanged_by_fix() -> None:
    """Tier 2: a text-only ChatMessage (str content, no image, no tool_calls)
    is counted IDENTICALLY before and after the type-adapter fix — pins that
    the fix changes only the image/tool_calls under-count, not plain text.
    """
    msg = ChatMessage(role="user", content="a" * 400, seq=1)
    # "Before fix" behaviour = calling estimate_tokens_for_turn directly on
    # the ChatMessage (the buggy call shape every real caller used to use).
    before = estimate_tokens_for_turn(msg, model="", use_chars4=True)
    after = estimate_tokens_for_any_turn(msg, model="", use_chars4=True)
    assert before == after == 100


# ---------------------------------------------------------------------------
# estimate_tokens_for_any_turn — image witness (real behaviour change)
# ---------------------------------------------------------------------------


def test_image_content_part_now_counted_at_fixed_cost() -> None:
    """Tier 2: witness — a ChatMessage carrying an image content part was
    undercounted (~a few tokens, via the .text fallback) before the fix;
    the adapter now surfaces the image part so _IMAGE_FIXED_TOKEN_COST
    applies — real ChatMessage, no fake, base64 never materialised (the
    part carries a plain data-url string, never read as bytes)."""
    msg = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 4000}},
        ],
        seq=1,
    )
    before = estimate_tokens_for_turn(msg, model="", use_chars4=True)
    after = estimate_tokens_for_any_turn(msg, model="", use_chars4=True)
    assert before < 10, f"pre-fix call shape should undercount (~text fallback), got {before}"
    assert after >= _IMAGE_FIXED_TOKEN_COST, (
        f"post-fix adapter must apply the fixed image cost, got {after}"
    )


# ---------------------------------------------------------------------------
# estimate_tokens_for_any_turn — tool_calls witness (real behaviour change)
# ---------------------------------------------------------------------------


def test_tool_calls_now_counted() -> None:
    """Tier 2: witness — a ChatMessage assistant turn with tool_calls and empty
    text content was previously counted as ~0 tokens (tool_calls ignored);
    the adapter folds tool_calls in as extra content parts so their JSON
    size now contributes to the estimate."""
    big_args = "x" * 4000
    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[
            {"id": "t1", "type": "function",
             "function": {"name": "big_tool", "arguments": big_args}},
        ],
        seq=1,
    )
    before = estimate_tokens_for_turn(msg, model="", use_chars4=True)
    after = estimate_tokens_for_any_turn(msg, model="", use_chars4=True)
    assert before <= 1, f"pre-fix call shape should ignore tool_calls, got {before}"
    assert after > before, "post-fix adapter must count the tool_calls payload"


# ---------------------------------------------------------------------------
# trim_head / trim_tail — ChatMessage witness (shares the same hole, #2957
# PR-A constraint 4)
# ---------------------------------------------------------------------------


def _big_image_message(seq: int) -> ChatMessage:
    return ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 100}}],
        seq=seq,
    )


def test_trim_head_now_bounded_by_image_cost() -> None:
    """Tier 2: witness — trim_head (via _trim_groups -> estimate_tokens_for_any_turn)
    now counts image turns at the fixed cost, so a budget sized to hold only
    ONE image-bearing turn excludes the later ones. Before the fix, image
    turns were near-zero cost and trim_head would have kept all three."""
    turns = [_big_image_message(1), _big_image_message(2), _big_image_message(3)]
    # Budget for slightly more than one image turn but well under two.
    budget = _IMAGE_FIXED_TOKEN_COST + 10
    result = trim_head(turns, max_tokens=budget, model="", use_chars4=True)
    kept_seqs = {t.seq for t in result}
    assert kept_seqs == {1}, (
        f"expected only the first image turn (seq=1) kept under a ~1-image "
        f"budget, got seqs={kept_seqs}"
    )


def test_trim_tail_now_bounded_by_image_cost() -> None:
    """Tier 2: witness — trim_tail shares _trim_groups with trim_head — same
    image-cost fix applies from the tail end."""
    turns = [_big_image_message(1), _big_image_message(2), _big_image_message(3)]
    budget = _IMAGE_FIXED_TOKEN_COST + 10
    result = trim_tail(turns, max_tokens=budget, model="", use_chars4=True)
    kept_seqs = {t.seq for t in result}
    assert kept_seqs == {3}, (
        f"expected only the last image turn (seq=3) kept under a ~1-image "
        f"budget, got seqs={kept_seqs}"
    )
