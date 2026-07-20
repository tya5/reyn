"""Tier 2: #2957 PR-B — elide-side and advisor-side token accounting unified
on the ``_serialise_turn`` wire-dict canonical.

Before PR-B, ``RouterHistoryBuffer.build_history`` measured its elide-
threshold total over raw ChatMessage instances (serialise-INPUT) while
``ContextBudgetAdvisor._incremental_history_tokens`` measured
``json.dumps(build_history()'s own output)`` (serialise-OUTPUT) — two
different quantities for the same conversation, with the advisor's
json.dumps additionally counting an inlined image's FULL base64 payload as
text instead of the ``_IMAGE_FIXED_TOKEN_COST`` fixed cost the elide side
(post #2957 PR-A) applied. PR-B makes both sides sum
``estimate_tokens_for_turn`` over the SAME wire dicts (``_serialise_turn``'s
output).

Per the co-vet addendum on this PR: a naive "same function on the same
input" consistency assertion is a tautology that stays green even if the
unification were reverted, UNLESS it is exercised on inputs where the two
PRE-PR-B measurement schemes would actually have disagreed. Every test below
targets one such condition:

  1. an image-bearing conversation (base64 payload vs fixed cost — the #2957
     PR-A gap, now closed on BOTH sides)
  2. a tool_calls-bearing conversation (ignored entirely by the pre-PR-A/
     pre-PR-B json.dumps-agnostic paths in one shape or another)
  3. a conversation that actually ELIDES (proves the circularity is closed —
     the advisor's number reflects the POST-elide reality, not a stale
     pre-elide figure, and still matches a from-scratch canonical recompute
     over that same post-elide output)
  4. the SAME image content before vs after path-ref materialisation (an
     un-materialised ``{"type":"image",...}`` path-ref part and its
     materialised ``{"type":"image_url",...}`` data-URL form must count
     IDENTICALLY — both hit the same fixed-cost branch)

Real ``RouterHistoryBuffer`` + real ``ContextBudgetAdvisor`` + real
``ChatMessage`` + real ``MediaStore`` throughout — no fakes (#2957 PR-A's own
lesson: a hand-rolled ``_FakeMessage`` hid a real production bug).
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.services.compaction.engine import (
    _IMAGE_FIXED_TOKEN_COST,
    estimate_tokens_for_any_turn,
)

_MODEL = "gpt-3.5-turbo"


def _make_pair(history: list[ChatMessage], *, media_store=None, use_chars4: bool = True):
    """Real RouterHistoryBuffer + real ContextBudgetAdvisor wired exactly as
    production wires them (ContextBudgetAdvisor.history_fn = build_history —
    see that class's own module docstring). Returns (buf, advisor, events) —
    the shared EventLog is exposed so a caller can observe build_history's
    own internal ``history_elide_total_computed`` audit-event (see
    ``test_elide_total_advisor_and_reference_three_way_agree`` below)."""
    cfg = CompactionConfig(use_chars4_estimate=use_chars4)
    events = EventLog()
    buf = RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=cfg,
        compaction_controller=None,  # → get_max_input_tokens fallback path
        model_fn=lambda: _MODEL,
        events=events,
        media_store=media_store,
        router_host=None,
        action_retrieval=None,
        non_interactive=True,
    )
    advisor = ContextBudgetAdvisor(
        compaction=cfg,
        compaction_controller=None,
        media_store=media_store,
        model_fn=lambda: _MODEL,
        events=events,
        history_fn=buf.build_history,
    )
    return buf, advisor, events


def _canonical_reference_tokens(wire_turns: list[dict], use_chars4: bool) -> int:
    """From-scratch sum of estimate_tokens_for_any_turn over already-
    serialised wire dicts — the canonical quantity both build_history's
    elide-threshold check and the advisor are supposed to converge on.
    estimate_tokens_for_any_turn (not the dict-only estimate_tokens_for_turn
    directly) is required because a wire dict's tool_calls lives in a
    separate top-level key, not inside "content"."""
    return sum(
        estimate_tokens_for_any_turn(w, _MODEL, use_chars4=use_chars4) for w in wire_turns
    )


# ---------------------------------------------------------------------------
# 1. Image-bearing conversation, no elide
# ---------------------------------------------------------------------------


def test_image_bearing_conversation_elide_and_advisor_agree():
    """Tier 2: witness 1 — an image-bearing conversation. The advisor's
    number must equal a from-scratch canonical recompute over
    build_history's own output, AND must be near the small fixed-cost
    regime (proving it is NOT counting the base64 payload as text — the
    pre-PR-B advisor bug)."""
    big_b64 = "x" * 40_000  # a real base64 payload would dwarf any fixed cost
    history = [
        ChatMessage(role="user", content="hi", seq=1),
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}},
            ],
            seq=2,
        ),
        ChatMessage(role="assistant", content="ok", seq=3),
    ]
    buf, advisor, _events = _make_pair(history)

    wire = buf.build_history()
    assert len(wire) == len(history), "sanity: small conversation must not elide"

    advisor_tokens = advisor._incremental_history_tokens()
    reference = _canonical_reference_tokens(wire, use_chars4=True)

    assert advisor_tokens == reference, (
        f"advisor ({advisor_tokens}) must match the canonical wire-dict "
        f"recompute ({reference})"
    )
    # Falsifies the pre-PR-B json.dumps(combined) advisor scheme: that would
    # have counted len(big_b64)//4 ≈ 10_000 tokens for the base64 text alone,
    # dwarfing the fixed-cost regime this assertion pins.
    assert advisor_tokens < len(big_b64) // 4, (
        f"advisor_tokens={advisor_tokens} looks like it counted the base64 "
        f"payload as text (pre-PR-B bug), not the fixed image cost"
    )
    assert advisor_tokens >= _IMAGE_FIXED_TOKEN_COST


# ---------------------------------------------------------------------------
# 2. tool_calls-bearing conversation
# ---------------------------------------------------------------------------


def test_tool_calls_bearing_conversation_elide_and_advisor_agree():
    """Tier 2: witness 2 — an assistant turn with tool_calls and no text.
    Falsifies a scheme where tool_calls are dropped from one side's count:
    both sides must reflect the tool_calls payload identically."""
    big_args = "y" * 5000
    history = [
        ChatMessage(role="user", content="run the tool", seq=1),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {"id": "t1", "type": "function",
                 "function": {"name": "big_tool", "arguments": big_args}},
            ],
            seq=2,
        ),
        ChatMessage(role="tool", content="result", tool_call_id="t1", name="big_tool", seq=3),
    ]
    buf, advisor, _events = _make_pair(history)

    wire = buf.build_history()
    advisor_tokens = advisor._incremental_history_tokens()
    reference = _canonical_reference_tokens(wire, use_chars4=True)

    assert advisor_tokens == reference
    # Falsifies "tool_calls ignored" — the arguments payload must actually
    # contribute (chars4 estimate of ~5000 chars is well over a few tokens).
    assert advisor_tokens > 1000, (
        f"advisor_tokens={advisor_tokens} looks like it ignored tool_calls"
    )


# ---------------------------------------------------------------------------
# 3. Post-elide: the circularity witness
# ---------------------------------------------------------------------------


def test_post_elide_advisor_matches_canonical_recompute_of_the_elided_output(monkeypatch):
    """Tier 2: witness 3 — history large enough to actually ELIDE. This is
    the direct witness that the elide-side/advisor-side circularity is
    closed: pre-PR-B the advisor measured build_history's OUTPUT (already
    elided) while build_history's own trigger decision measured a DIFFERENT
    quantity (pre-serialise ChatMessage) — this test proves that after
    elide fires, the advisor's number is still an exact canonical recompute
    of what actually survived."""
    import reyn.llm.model_budget as _mb

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda *a, **kw: _IMAGE_FIXED_TOKEN_COST)

    def _big_image(seq: int) -> ChatMessage:
        return ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "z" * 200}}],
            seq=seq,
        )

    history = [_big_image(i) for i in range(1, 9)]
    buf, advisor, _events = _make_pair(history)

    wire = buf.build_history()
    assert len(wire) < len(history), "test premise: this conversation must actually elide"

    advisor_tokens = advisor._incremental_history_tokens()
    reference = _canonical_reference_tokens(wire, use_chars4=True)
    assert advisor_tokens == reference, (
        f"post-elide: advisor ({advisor_tokens}) must equal the canonical "
        f"recompute over the ACTUALLY-SURVIVING wire dicts ({reference}) — "
        f"a stale pre-elide total would diverge from this"
    )


# ---------------------------------------------------------------------------
# 4. Materialisation boundary: path-ref vs inline data URL count identically
# ---------------------------------------------------------------------------


def test_pathref_and_materialised_image_count_identically(tmp_path):
    """Tier 2: witness 4 — the SAME image, once as an un-materialised
    path-ref (``{"type":"image","path":...}``) and once already inline
    (``{"type":"image_url",...}``), must produce IDENTICAL wire-dict token
    counts. Both hit ``estimate_tokens_for_turn``'s fixed-cost branch
    (``"image"`` and ``"image_url"`` are both members of that branch's type
    set) — proves the canonical measure does not depend on whether
    materialisation has already run, so serialising for measurement
    purposes is safe regardless of which state a turn's content is in.
    """
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    pathref_block = store.save_image(raw, mime_type="image/png", tool="test", seq=1)
    assert pathref_block["type"] == "image"  # sanity: genuinely un-materialised

    history_pathref = [
        ChatMessage(role="user", content=[{"type": "text", "text": "look"}, pathref_block], seq=1),
    ]
    buf_pathref, advisor_pathref, _events_pathref = _make_pair(history_pathref, media_store=store)
    wire_pathref = buf_pathref.build_history()
    assert wire_pathref[0]["content"][1]["type"] == "image_url", (
        "sanity: build_history's real _serialise_turn must have materialised it"
    )
    tokens_pathref = advisor_pathref._incremental_history_tokens()

    materialised_block = wire_pathref[0]["content"][1]
    history_inline = [
        ChatMessage(role="user", content=[{"type": "text", "text": "look"}, materialised_block], seq=1),
    ]
    buf_inline, advisor_inline, _events_inline = _make_pair(history_inline, media_store=None)
    wire_inline = buf_inline.build_history()
    tokens_inline = advisor_inline._incremental_history_tokens()

    assert tokens_pathref == tokens_inline, (
        f"path-ref ({tokens_pathref}) and already-materialised ({tokens_inline}) "
        f"forms of the identical image must count identically"
    )


# ---------------------------------------------------------------------------
# 5. Co-vet follow-up — observe elide's OWN internal total, not just a
#    test-side reference recomputed from its returned wire dicts
# ---------------------------------------------------------------------------


def test_elide_total_advisor_and_reference_three_way_agree():
    """Tier 2: witness 5 — closes a gap in witnesses 1-4: those compare
    ``advisor_tokens == _canonical_reference_tokens(wire)``, which is a
    comparison between the advisor and a TEST-SIDE reference recomputed
    from build_history's returned wire dicts — it never observes what
    ``build_history`` itself counted internally to make its elide/no-elide
    decision.

    Content choice matters here, not just the observation point: an
    image_url/tool_calls fixture (witnesses 1-2's content) turns out NOT to
    discriminate a reverted elide-side total, because
    ``estimate_tokens_for_any_turn`` already produces the IDENTICAL number
    for a raw ``ChatMessage`` and its serialised wire dict in that case (by
    design — that's what makes the adapter correct for both shapes). This
    test instead uses an UNRESOLVABLE path-ref image
    (``{"type": "image", "path": <nonexistent file>}``): the raw
    ``ChatMessage`` counts a full ``_IMAGE_FIXED_TOKEN_COST`` "phantom"
    image that will NEVER reach the provider, while ``_serialise_turn``
    DROPS the unresolvable block entirely (see
    ``_materialise_path_ref_content`` / ``_read_pathref_image`` — a missing
    file returns ``None`` and the content part is omitted) — so the wire
    dict genuinely has fewer tokens than the raw ChatMessage. This is a
    real, content-driven divergence between serialise-INPUT and
    serialise-OUTPUT counting, not just an architectural symmetry — exactly
    the class of case #2957 PR-B's "measure the canonical wire quantity"
    design decision is FOR.

    ``build_history`` emits its internal total as a public P6 audit-event
    (``history_elide_total_computed`` — see that method) precisely so this
    is observable without touching private state (CLAUDE.md: no private-
    state assertions). This test asserts the 3-way agreement: elide's own
    emitted total == advisor's measurement == the canonical reference.
    """
    history = [
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "look"},
                {"type": "image", "path": "/nonexistent/2957-prb-witness/does-not-exist.png",
                 "mime_type": "image/png"},
            ],
            seq=1,
        ),
    ]
    buf, advisor, events = _make_pair(history, media_store=None)

    wire = buf.build_history()
    assert wire[0]["content"] == [{"type": "text", "text": "look"}], (
        "test premise: the unresolvable path-ref block must be DROPPED at "
        "serialise time, so the wire dict is strictly smaller than the raw "
        "ChatMessage's content"
    )

    elide_events = [e for e in events.all() if e.type == "history_elide_total_computed"]
    assert elide_events, (
        "build_history must emit its internal elide-threshold total as a "
        "public audit-event — without this, elide's own accounting is "
        "unobservable from outside private state"
    )
    elide_total = elide_events[-1].data["total"]

    advisor_tokens = advisor._incremental_history_tokens()
    reference = _canonical_reference_tokens(wire, use_chars4=True)

    assert elide_total == advisor_tokens == reference, (
        f"3-way mismatch: elide's own total={elide_total}, "
        f"advisor={advisor_tokens}, reference={reference}"
    )
