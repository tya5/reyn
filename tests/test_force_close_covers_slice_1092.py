"""Tier 2: durable force-close covers-respecting slice (#1092 PR-F2a).

F2a is the slice mechanism the F2b chat handoff stands on. When the latest chat
summary is a force-close handoff consolidation (identified by the dedicated
``consolidation`` structured field), ``_build_history_for_router`` DROPS the
covered raw head/tail and slices ``[consolidation bridge] + turns appended after
the consolidation`` — the durable "true reset" (re-applied every turn, so a
subsequent turn never re-slices the dropped raw history → no re-overflow). This
is the chat analogue of phase's checkpoint-reset (D2).

★Gated: only a force-close consolidation triggers the drop. Normal compaction
summaries (no ``consolidation`` field) fall through to the unchanged
head/tail+bridge path → normal chat stays byte-identical (pinned below + by the
unchanged test_session_router_history_slicing suite).

No mocks: a real ChatSession with a synthetic T_max.
"""
from __future__ import annotations

from reyn.chat.session import (
    ChatMessage,
    _is_force_close_consolidation,
    _render_summary_for_storage,
)
from tests.test_session_router_history_slicing import _make_session, _now, _push


def _append_force_close_summary(session, consolidation: str) -> ChatMessage:
    """Append a force-close consolidation summary (the F2b install shape)."""
    msg = ChatMessage(
        role="summary",
        content=_render_summary_for_storage({"consolidation": consolidation}),
        ts=_now(),
        meta={"structured": {"consolidation": consolidation}, "covers_through_seq": 0},
    )
    session.history.append(msg)
    return msg


# ── the durable reset: covered raw head/tail dropped ─────────────────────────


def test_force_close_consolidation_drops_covered_head_tail(tmp_path) -> None:
    """Tier 2: with a force-close consolidation as the latest summary, the slice
    is [consolidation bridge] only — the covered raw head/tail is dropped (the
    true reset), and the consolidation text reaches the prompt."""
    session = _make_session(tmp_path)
    for t in ("U1-covered", "A1-covered", "U2-covered", "A2-covered"):
        _push(session, "user" if t.startswith("U") else "assistant", t)
    _append_force_close_summary(session, "CONSOLIDATED-ESSENCE")

    msgs = session._build_history_for_router()
    blob = "\n".join(m["content"] for m in msgs if isinstance(m.get("content"), str))
    assert "CONSOLIDATED-ESSENCE" in blob          # consolidation reaches the slice
    for covered in ("U1-covered", "A1-covered", "U2-covered", "A2-covered"):
        assert covered not in blob                  # covered raw head/tail dropped


def test_post_consolidation_turns_retained_including_assistant(tmp_path) -> None:
    """Tier 2: turns appended AFTER the consolidation are retained — including an
    assistant turn (seq=0). Pins the position-based (not seq>covers) filter: a
    seq filter would wrongly drop post-handoff assistant replies."""
    session = _make_session(tmp_path)
    _push(session, "user", "U1-covered")
    _push(session, "assistant", "A1-covered")
    _append_force_close_summary(session, "CONSOL")
    _push(session, "user", "U2-after")
    _push(session, "assistant", "A2-after-assistant")  # seq=0

    msgs = session._build_history_for_router()
    blob = "\n".join(m["content"] for m in msgs if isinstance(m.get("content"), str))
    assert "CONSOL" in blob
    assert "U2-after" in blob
    assert "A2-after-assistant" in blob             # seq=0 assistant retained
    assert "U1-covered" not in blob                 # pre-consolidation dropped
    assert "A1-covered" not in blob


# ── the gate: normal compaction summaries unaffected (byte-identical) ──────────


def test_normal_compaction_summary_does_not_trigger_reset(tmp_path) -> None:
    """Tier 2: a NORMAL compaction summary (no `consolidation` field) does NOT
    trigger the force-close reset — the raw turns still appear (head/tail+bridge
    behaviour unchanged). With a large T_max (no elide) all raw turns are
    returned, proving the reset branch did not fire."""
    session = _make_session(tmp_path)  # default T_max huge → no elide
    _push(session, "user", "U1-raw")
    _push(session, "assistant", "A1-raw")
    session.history.append(ChatMessage(
        role="summary", content="[topic] x", ts=_now(),
        meta={"structured": {"topic_arc": "x"}, "covers_through_seq": 0},
    ))

    msgs = session._build_history_for_router()
    blob = "\n".join(m["content"] for m in msgs if isinstance(m.get("content"), str))
    assert "U1-raw" in blob and "A1-raw" in blob    # raw turns NOT dropped


# ── unit: detection + renderer ───────────────────────────────────────────────


def test_is_force_close_consolidation_detection() -> None:
    """Tier 2: the gate keys on the dedicated `consolidation` structured field."""
    fc = ChatMessage(role="summary", content="", ts=_now(),
                     meta={"structured": {"consolidation": "x"}})
    normal = ChatMessage(role="summary", content="", ts=_now(),
                         meta={"structured": {"topic_arc": "x"}})
    no_meta = ChatMessage(role="summary", content="", ts=_now(), meta=None)
    assert _is_force_close_consolidation(fc) is True
    assert _is_force_close_consolidation(normal) is False
    assert _is_force_close_consolidation(no_meta) is False


def test_render_consolidation_field_verbatim_and_normal_unchanged() -> None:
    """Tier 2: the renderer surfaces the consolidation verbatim; a normal
    structured dict (no consolidation) renders exactly as before (byte-identical)."""
    assert "MY-CONSOLIDATION" in _render_summary_for_storage(
        {"consolidation": "MY-CONSOLIDATION"}
    )
    # normal summary: unchanged output (no `consolidation` → no new lines).
    assert _render_summary_for_storage({"topic_arc": "T"}) == "[topic] T"
    assert _render_summary_for_storage(
        {"decisions": ["d1"]}
    ) == "[decisions]\n  - d1"
