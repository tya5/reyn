"""Tier 1/2: #4680 ② — ``get_max_input_tokens``'s fallback state split.

Two states, previously conflated into the same value AND the same message
(the issue's own reported symptom): ``NOT_READY`` (litellm hasn't finished
importing in this process yet — TEMPORARY) vs ``UNCATALOGED`` (litellm IS
loaded but has no entry for this model — PERMANENT). Covers the pure
``MaxInputTokensFallbackReason`` split, the warning-message split, the
correction-on-resolve requirement (lead-coder's own required condition:
"a warning correction in the SAME PR"), and the event payload's new
``reason`` field.

Real ``litellm`` module + a real, scripted stand-in for
``ensure_litellm_ready_or_defer`` (a plain function raising the REAL
``LitellmWarmingInBackgroundError``, mirroring ``test_model_class_ceiling_
4206.py``'s own ``_spy``/``_fake`` pattern) so the NOT_READY leg is
deterministic rather than racing the real background warm thread — the
real-timing race is covered separately by ``test_model_budget.py``'s
``ensure_litellm_ready()``-blocked catalog tests and by #4680②'s own issue
comment (100%-reproducible empirical measurement).
"""
from __future__ import annotations

import logging

import pytest

from reyn.core.events.events import EventLog
from reyn.llm.model_budget import (
    _FALLBACK_MAX_INPUT_TOKENS,
    MaxInputTokensFallbackReason,
    _resolve_max_input,
    _warned_models,
    get_max_input_tokens,
    get_max_input_tokens_source,
)
from tests._support.events import collect_events


@pytest.fixture(autouse=True)
def _clear_warned_models():
    """Tier fixture: ``_warned_models`` is process-shared (by design, same
    scope ``_config_max_input_overrides`` uses) — clear it before/after
    each test in this file so one test's warm/cold state can't leak into
    the next one's assertions. Uses unique-ish model names besides, but
    this removes any doubt."""
    _warned_models.clear()
    yield
    _warned_models.clear()


@pytest.fixture(autouse=True)
def _litellm_actually_ready():
    """Tier fixture: blocks on the REAL, non-mocked ``ensure_litellm_ready``
    once per test — the same fix ``test_model_budget.py``'s own catalog
    tests needed (#4680② own measurement: a test that wants the GENUINE
    UNCATALOGED path, not a monkeypatched NOT_READY, is itself
    order-dependent on whether an EARLIER test/process activity has
    already triggered a real ``import litellm`` — a test file where every
    other NOT_READY case is monkeypatched (never touching the real
    background-warm machinery) can leave litellm never actually
    triggered at all, so the FIRST genuinely-real call in the file hits
    the real cold path instead of the catalog it means to test). Not a
    sleep — waits on the real condition, unbounded, per testing.md §
    Time; cheap after the first call (``ensure_litellm_ready``'s own
    fast-path early return)."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready
    ensure_litellm_ready()


def _raise_not_ready(*args, **kwargs):
    from reyn.llm.litellm_bootstrap import LitellmWarmingInBackgroundError
    raise LitellmWarmingInBackgroundError("litellm is warming in the background")


# ---------------------------------------------------------------------------
# MaxInputTokensFallbackReason split — pure resolution (Tier 1: contract)
# ---------------------------------------------------------------------------


def test_resolve_max_input_reports_not_ready_reason(monkeypatch) -> None:
    """Tier 1: contract — when litellm hasn't finished importing yet
    (``LitellmWarmingInBackgroundError``), ``_resolve_max_input`` reports
    ``NOT_READY``, not the generic/old undifferentiated fallback."""
    monkeypatch.setattr(
        "reyn.llm.litellm_bootstrap.ensure_litellm_ready_or_defer", _raise_not_ready,
    )
    value, source, reason = _resolve_max_input("some/model-4680-2-a")
    assert value == _FALLBACK_MAX_INPUT_TOKENS
    assert reason is MaxInputTokensFallbackReason.NOT_READY
    assert "not yet loaded" in source


def test_resolve_max_input_reports_uncataloged_reason() -> None:
    """Tier 1: contract — a genuinely-unrecognized model (litellm IS
    loaded/reachable — no NOT_READY monkeypatch here) reports
    ``UNCATALOGED``."""
    value, source, reason = _resolve_max_input("unknown/garbage-4680-2-b")
    assert value == _FALLBACK_MAX_INPUT_TOKENS
    assert reason is MaxInputTokensFallbackReason.UNCATALOGED
    assert "not cataloged" in source


def test_resolve_max_input_config_override_reports_no_fallback_reason() -> None:
    """Tier 1: accept-side — an operator-declared override (#4689) is not
    a fallback at all; ``reason`` is ``None``."""
    from reyn.llm.model_budget import register_max_input_overrides

    register_max_input_overrides({"some/configured-model-4680-2": 55_555})
    try:
        value, _source, reason = _resolve_max_input("some/configured-model-4680-2")
        assert value == 55_555
        assert reason is None
    finally:
        # No unregister API exists; use a unique model name per test run
        # instead of trying to clean the process-shared registry up.
        pass


# ---------------------------------------------------------------------------
# get_max_input_tokens_source — the real, already-wired UI consumer
# (context_budget_advisor's status-bar chip) sees the split (Tier 2)
# ---------------------------------------------------------------------------


def test_source_distinguishes_not_ready_from_uncataloged(monkeypatch) -> None:
    """Tier 2: THE observation-split witness — the SAME consumer
    (``get_max_input_tokens_source``, what the status-bar ctx chip reads)
    returns TEXTUALLY DIFFERENT strings for the two states, where before
    #4680② both said the identical "model not cataloged"."""
    monkeypatch.setattr(
        "reyn.llm.litellm_bootstrap.ensure_litellm_ready_or_defer", _raise_not_ready,
    )
    not_ready_source = get_max_input_tokens_source("some/model-4680-2-c")

    monkeypatch.undo()
    uncataloged_source = get_max_input_tokens_source("unknown/garbage-4680-2-d")

    assert not_ready_source != uncataloged_source
    assert "not yet loaded" in not_ready_source
    assert "not cataloged" in uncataloged_source


# ---------------------------------------------------------------------------
# Warning message split + event reason field (Tier 2)
# ---------------------------------------------------------------------------


def test_not_ready_warning_message_says_temporary(monkeypatch, caplog) -> None:
    """Tier 2: the log warning for NOT_READY explicitly says it is
    temporary/self-correcting — distinct wording from UNCATALOGED's
    permanent framing."""
    monkeypatch.setattr(
        "reyn.llm.litellm_bootstrap.ensure_litellm_ready_or_defer", _raise_not_ready,
    )
    with caplog.at_level(logging.WARNING):
        get_max_input_tokens("some/model-4680-2-e")

    assert any(
        "not finished loading" in r.message and "self-correct" in r.message
        for r in caplog.records
    )


def test_uncataloged_warning_message_says_permanent(caplog) -> None:
    """Tier 2: the log warning for UNCATALOGED explicitly says it is
    permanent for this process — distinct wording from NOT_READY's
    temporary framing. Uses the standard ``caplog`` fixture (not a
    hand-attached logging.Handler + logger.setLevel — an earlier draft of
    this test did that and LEAKED the logger's level across the rest of
    the process, since ``logger.setLevel`` was never restored, silently
    swallowing every subsequent test's INFO-level correction-log
    assertion in this same file)."""
    with caplog.at_level(logging.WARNING):
        get_max_input_tokens("unknown/garbage-4680-2-f")

    assert any(
        "permanent for this process" in r.message for r in caplog.records
    )


def test_fallback_event_carries_reason_field() -> None:
    """Tier 2: the ``model_budget_fallback`` audit-event payload carries
    the NEW ``reason`` field (``"uncataloged"``/``"not_ready"``) — a
    payload-field addition on the SAME existing kind, not a new kind
    (CLAUDE.md's closed-vocabulary rule governs kinds, not fields)."""
    events = EventLog()
    collected = collect_events(events)
    get_max_input_tokens("unknown/garbage-4680-2-g", events=events)

    fallback_events = [e for e in collected if e.type == "model_budget_fallback"]
    assert fallback_events
    assert fallback_events[0].data["reason"] == "uncataloged"


# ---------------------------------------------------------------------------
# THE correction requirement (lead-coder's own required condition,
# verbatim): "NotReadyで1回警告した後、catalogがwarmになって値が変わったら
# 言う" — a NOT_READY warning must be corrected once the SAME model later
# resolves for real. (Tier 2)
# ---------------------------------------------------------------------------


def test_not_ready_then_resolved_emits_a_correction_log(monkeypatch, caplog) -> None:
    """Tier 2: THE mandatory correction witness — a model warned as
    NOT_READY, then called again once litellm is (simulated) ready and
    resolves to a real catalog value, gets a WARNING-level correction log
    naming the resolved value. "Warned once, never corrected" (#4680's
    own reported symptom) is wrong for this temporary case.

    #4805: the capture floor below is WARNING, not INFO, DELIBERATELY —
    the interactive CUI's own production floor (`interfaces/cli/commands/
    chat.py`'s `basicConfig(level=WARNING, force=True)`) discards
    anything below it, so a test that captured at INFO would stay green
    even if this correction log were invisible in real production (the
    exact "green but silent in production" defect #4805 exists to close
    — this correction log used to be `logger.info(...)`, passing this
    same test under a `caplog.at_level(logging.INFO)` capture while
    genuinely emitting nothing under the real WARNING floor). Capturing
    at WARNING here means this test can only pass if the log actually
    clears the SAME floor production uses."""
    model = "gemini/gemini-2.5-flash-lite"  # a real, cataloged model

    monkeypatch.setattr(
        "reyn.llm.litellm_bootstrap.ensure_litellm_ready_or_defer", _raise_not_ready,
    )
    with caplog.at_level(logging.WARNING):
        first = get_max_input_tokens(model)
    assert first == _FALLBACK_MAX_INPUT_TOKENS
    assert any("not finished loading" in r.message for r in caplog.records)
    caplog.clear()

    monkeypatch.undo()  # litellm is "ready" again — real catalog lookup
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready
    ensure_litellm_ready()  # deterministic — real litellm is actually warm

    with caplog.at_level(logging.WARNING):
        second = get_max_input_tokens(model)
    assert second != _FALLBACK_MAX_INPUT_TOKENS
    assert any(
        "is now resolved via litellm catalog" in r.message for r in caplog.records
    ), (
        "the correction must be visible at the interactive CUI's own "
        "production floor (WARNING) — not merely at a lower capture "
        "level a real session never uses"
    )


def test_uncataloged_never_gets_a_correction_log_on_repeat_calls(caplog) -> None:
    """Tier 2: accept-side of the same witness — a model warned as
    UNCATALOGED (permanent) does NOT get a spurious "correction" log on a
    later call for the same model; the "warn once, stay quiet" contract
    is still correct for the permanent state (unchanged by #4680②)."""
    model = "unknown/garbage-4680-2-h"

    with caplog.at_level(logging.WARNING):
        get_max_input_tokens(model)
    assert any("permanent for this process" in r.message for r in caplog.records)
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        get_max_input_tokens(model)
    assert not any(
        "is now resolved via litellm catalog" in r.message for r in caplog.records
    ), "UNCATALOGED must never emit a correction — it is a permanent state"
    assert not any(
        "not finished loading" in r.message or "permanent for this process" in r.message
        for r in caplog.records
    ), "the second call for the SAME already-warned model must stay silent"


def test_not_ready_then_uncataloged_transition_warns_again_with_new_reason(
    monkeypatch, caplog,
) -> None:
    """Tier 2: accept-side — a model warned NOT_READY, then on a later
    call litellm IS ready but the model turns out genuinely uncataloged:
    this is a FRESH reason (not a silent repeat), so it warns again with
    the UNCATALOGED wording — not treated as already-warned."""
    model = "unknown/garbage-4680-2-i"

    monkeypatch.setattr(
        "reyn.llm.litellm_bootstrap.ensure_litellm_ready_or_defer", _raise_not_ready,
    )
    with caplog.at_level(logging.WARNING):
        get_max_input_tokens(model)
    assert any("not finished loading" in r.message for r in caplog.records)
    caplog.clear()

    monkeypatch.undo()
    with caplog.at_level(logging.WARNING):
        get_max_input_tokens(model)
    assert any("permanent for this process" in r.message for r in caplog.records)
