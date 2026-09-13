"""Tier 2: #5890 §2 — the reactive shrink ladder's rung① (spill, at the
mid-turn floor) now offers ``tail`` to ``spill_fn`` alongside ``raw_middle``'s
own slice, not ``raw_middle`` alone.

Root cause (architect + lead-coder ruling, correcting the original "reorder
spill before refill" brief — spill already runs before refill; what was
missing is what spill SEARCHES): ``offered`` (``RecoveryLadder.
_run_one_iteration``, ``engine.py``) used to be built exclusively from
``raw_middle``'s own offered prefix — a genuinely oversized/pathological
``tail`` turn was structurally invisible to this rung, no matter how
eligible its own declared ``Spillability`` was.

This file's own positive control (required by review): ``is_already_spilled``
is checked BY VALUE (that method's own docstring), so a turn spilled once —
regardless of which list (``raw_middle`` or ``tail``) it started in — reads
as already-spilled on any LATER re-scan from either list. This is what makes
widening the population safe without adding any new state: a future refill
moving a tail-spilled turn into ``raw_middle`` cannot make measure ④ (the
population's own progress) go backward — the SAME content is recognised as
already-spilled wherever it is re-encountered.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer


def _make_real_history_buffer(tmp_path):
    """Minimal real ``RouterHistoryBuffer`` + ``MediaStore`` — cheap to
    construct (no I/O beyond what ``spill_turn_content`` itself performs),
    mirrors ``test_5720_spill_carries_real_turn_provenance.py``'s own
    established recipe for this exact pair."""
    history: list = []
    store = MediaStore(
        project_root=tmp_path, agent_name="t-agent", session_id="t-session",
    )
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=None,
        model_fn=lambda: "test-model",
        events=EventLog(),
        media_store=store,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=True,
        history_appender=history.append,
    ), history


def test_is_already_spilled_returns_false_for_a_tail_turns_original_content(tmp_path):
    """Tier 2: #5890 §2 positive control (required by review) — a turn's
    ORIGINAL, never-spilled content reads as NOT already-spilled,
    regardless of which list (``raw_middle`` vs ``tail``) it is being
    checked FROM. ``is_already_spilled`` takes no turn identity or origin
    at all (its own docstring: "Checked by VALUE ... the same content
    string could arrive from a different candidate object") — this test
    exercises it with a plain ``tail``-shaped turn's own content, never
    having gone through ``spill_turn_content``, and confirms it is NOT
    mistaken for an already-spilled preview.

    This is the exact property #5890 §2's own design depends on: if this
    ever returned ``True`` for original content, widening ``offered`` to
    include ``tail`` would make the spill rung skip genuinely-fresh tail
    candidates as if they were already handled — the opposite failure
    from the one the widening is meant to fix.
    """
    history_buffer, _history = _make_real_history_buffer(tmp_path)

    tail_turn_original_content = "a perfectly ordinary tail turn, never spilled"

    assert history_buffer.is_already_spilled(tail_turn_original_content) is False


def test_is_already_spilled_recognises_content_spilled_from_a_tail_turn(tmp_path):
    """Tier 2: #5890 §2 — the OTHER half of the same value-based
    guarantee: once a turn's content genuinely IS spilled (regardless of
    which list it came from), ``is_already_spilled`` recognises the
    RESULT as already-spilled. This is what closes the "tail-spilled
    turn later re-scanned as mid" hazard #5890 §2's own review named: a
    future refill moving this exact content into ``raw_middle`` would
    still correctly skip it here, by value, never re-spilling it and
    never double-counting it as fresh progress."""
    history_buffer, _history = _make_real_history_buffer(tmp_path)

    original_content = "TAIL_OVERSIZED_RESULT " * 2000
    assert history_buffer.is_already_spilled(original_content) is False

    spilled_preview = history_buffer.spill_turn_content(
        original_content, chain_id="c1", tool="tail_turn", seq=1,
    )
    assert spilled_preview is not None, "expected a real spill (media_store is configured)"
    assert spilled_preview != original_content

    assert history_buffer.is_already_spilled(spilled_preview) is True
    # The ORIGINAL content (as opposed to the preview it turned into) is
    # of course no longer present anywhere — this checks the ONE thing
    # #5890 §2 depends on: the preview itself must not be re-offered.
