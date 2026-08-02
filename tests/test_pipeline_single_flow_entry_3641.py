"""Tier 2: a pipeline run occupies ONE row, and that row shows its progress.

A 15-step run emits a ``pipeline_step_started``/``pipeline_step_completed``
pair per step — 30 frames — and each was appended as its own flow entry, so the
conversation the run belongs to scrolled away behind its own progress.

The frames were already the right shape for folding: ``lifecycle_forwarder``
sends them as transient ``status`` carrying the ``run_id``. Nothing consumed
the key. These pin both halves of the fix — one row per run, and a row that
reports the run's state rather than the latest frame's sentence.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat._meta_keys import PIPELINE_RUN_KEY
from reyn.interfaces.inline.textual_chat.presenter import _pipeline_row


def _step_meta(index: int, *, total: "int | None" = 15, completed: bool = False) -> dict:
    return {
        PIPELINE_RUN_KEY: "run-1",
        "pipeline_name": "rag_ingest.ingest",
        "step_index": index,
        "total_steps": total,
        "step_kind": "transform",
        "step_event": (
            "pipeline_step_completed" if completed else "pipeline_step_started"
        ),
    }


def test_the_row_names_the_run_and_where_it_is() -> None:
    """Tier 2: the pipeline, the count, and the step kind all reach the row."""
    row = _pipeline_row(_step_meta(6, completed=True)).plain

    assert "rag_ingest.ingest" in row
    assert "7/15" in row
    assert "transform" in row


def test_the_bar_tracks_progress() -> None:
    """Tier 2: the bar is a function of the numbers, not decoration.

    Compared across three points rather than pinned at one, so the assertion
    describes the relationship and survives a change to the bar's width.
    """
    start = _pipeline_row(_step_meta(0)).plain
    middle = _pipeline_row(_step_meta(6, completed=True)).plain
    end = _pipeline_row(_step_meta(14, completed=True)).plain

    assert start.count("━") == 0
    assert 0 < middle.count("━") < end.count("━")
    assert end.count("─") == 0


def test_an_unknown_total_gets_no_bar() -> None:
    """Tier 2: a bar over a guessed denominator is worse than none.

    ``total_steps`` is absent for any producer that does not know its length
    ahead of time; drawing a bar anyway would show a position on a scale that
    does not exist.
    """
    row = _pipeline_row(_step_meta(3, total=None)).plain

    assert "━" not in row and "─" not in row
    assert "step 4" in row or "step 3" in row


def test_the_row_does_not_read_the_frame_text() -> None:
    """Tier 2: rendering is driven by meta alone.

    The forwarder also composes a sentence, and reading that back would couple
    the display to its wording. Passing meta with no text at all is the direct
    statement that the text is not an input.
    """
    row = _pipeline_row(_step_meta(2)).plain

    assert "rag_ingest.ingest" in row  # rendered without any msg.text existing
