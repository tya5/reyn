"""Tier 2: #4573 — an unresolvable model CLASS in a class-position config
value (``llm.model``, ``llm.model_class_by_purpose.<purpose>``) warns at
LOAD time and still raises at USE time.

Architect ruling: two prior rulings looked like they conflicted (loader.py's
"warn, never hard-fail, anywhere" for unknown CONFIG KEYS vs #3368's
intentional hard-raise for an unresolvable model CLASS VALUE) — resolved not
by picking a side but by moving WHEN the raise happens, not whether:

  ① load  — warn (names the value, states the consequence: "NOT APPLIED
     until fixed")
  ② /model — a valid class switch works even from a broken initial state
  ③ use   — a genuine turn attempt with the unresolved value still fails,
     loudly, with the value and how to fix it (never silently degrades)
  ④ strip — reverting the try/except in ``try_build_default_turn_budget_
     engine`` back to catching only ``AssertionError`` must make ①'s own
     witness go RED (the crash #4573 reports, reproduced exactly)

Real ``ModelResolver``/``TurnBudgetEngine`` — no mocks, per the testing
policy. ``caplog`` for ①'s warning witness (the public, intended observation
surface for a logged warning — not a private-state read).
"""
from __future__ import annotations

import logging

import pytest

from reyn.llm.model_resolver import ModelResolver
from reyn.services.turn_budget import try_build_default_turn_budget_engine

# ── acceptance① — load-time warn, not raise ───────────────────────────────


def test_construction_with_an_unresolvable_default_class_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #4573's own reported crash — ``ModelResolver(...,
    default_class="gemini-2.5-flash-lite")`` where that string is neither a
    declared class nor a provider-prefixed name — must NOT raise at
    construction. RED pre-#4573 fix: this used to be silent (no crash here
    either, since #3368's raise lives in ``resolve()``, not ``__init__`` —
    but nothing warned, so the operator had no signal until whatever code
    happened to call ``resolve()`` first crashed instead)."""
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver(
            {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
            default_class="gemini-2.5-flash-lite",
        )
    assert any(
        "gemini-2.5-flash-lite" in r.message and "NOT APPLIED" in r.message
        for r in caplog.records
    ), f"expected a warning naming the value and its consequence, got: {[r.message for r in caplog.records]}"


def test_construction_with_an_unresolvable_purpose_class_also_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the SAME class-position shape, for a
    ``model_class_by_purpose`` entry — not just ``default_class``. Both are
    class positions ``resolve()`` treats identically; the load-time warn
    must too."""
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver(
            {"light": "openai/gpt-4o-mini"},
            default_class="light",
            purpose_classes={"router": "typo-d-class"},
        )
    assert any("typo-d-class" in r.message for r in caplog.records), (
        f"expected a warning naming the bad purpose-class value, got: "
        f"{[r.message for r in caplog.records]}"
    )


def test_construction_with_a_valid_default_class_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: falsification contrast — a resolvable ``default_class``
    produces NO class-position warning (the existing bare-name-position
    warning, #1454 PR-B, is a separate concern and not asserted against
    here)."""
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver(
            {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
            default_class="standard",
        )
    assert not any("NOT APPLIED" in r.message for r in caplog.records), (
        f"a valid default_class must not warn, got: {[r.message for r in caplog.records]}"
    )


def test_construction_with_a_slash_prefixed_default_class_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: falsification contrast — a literal name-position value
    (contains '/') is a legitimate passthrough, not an unresolved class;
    must not warn."""
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver(
            {"light": "openai/gpt-4o-mini"},
            default_class="anthropic/claude-opus-4",
        )
    assert not any("NOT APPLIED" in r.message for r in caplog.records)


# ── acceptance④ (checked first — it's the strip-falsify baseline the
# other engine-level tests below implicitly rely on) ──────────────────────


def test_try_build_degrades_to_none_for_an_unresolvable_model_class() -> None:
    """Tier 2: #4573's actual crash site — ``TurnBudgetEngine.__init__``
    calls ``resolver.resolve(model)`` directly, and (pre-fix)
    ``try_build_default_turn_budget_engine`` only caught ``AssertionError``,
    letting the ``ValueError`` from an unresolved class propagate straight
    through this LAZY, non-essential budget-estimation call — crashing the
    whole session before the operator could even reach ``/model``. Must now
    degrade to ``None`` (the same "legitimate permanent degrade" contract
    this function already has for a too-small-context model).

    Strip-falsify (acceptance④, done by hand rather than as an automated
    monkeypatch — see this test's own docstring for why): reverting the
    ``except`` clause in ``engine.py`` back to ``except AssertionError``
    alone makes THIS test raise instead of returning ``None`` — verified
    manually before this PR (not re-verified by a second, redundant test
    here — CLAUDE.md's own "same expression on both sides" caution about
    testing exactly the code under test)."""
    resolver = ModelResolver(
        {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
        default_class="gemini-2.5-flash-lite",
    )
    engine = try_build_default_turn_budget_engine(
        "gemini-2.5-flash-lite", resolver=resolver,
    )
    assert engine is None


def test_try_build_still_builds_normally_for_a_resolvable_class() -> None:
    """Tier 2: falsification contrast — a VALID class still produces a real
    engine (the fix does not silently degrade legitimate models too)."""
    resolver = ModelResolver(
        {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
        default_class="standard",
    )
    engine = try_build_default_turn_budget_engine("standard", resolver=resolver)
    assert engine is not None


def test_an_unrelated_value_error_still_propagates_through(monkeypatch) -> None:
    """Tier 2: architect blocking finding on the FIRST version of this fix
    (issuecomment-5385388810, PR #5212 A): ``except (AssertionError,
    ValueError)`` caught the SHAPE of the known failure (a bare
    ``ValueError``), not its CAUSE — a future, UNRELATED ``ValueError``
    raised anywhere else inside ``build_default_turn_budget_engine``'s own
    call chain would have ALSO silently degraded to ``None`` instead of
    propagating. Narrowed to
    :class:`~reyn.llm.model_resolver.UnresolvableModelClassError`
    specifically (a ``ValueError`` subclass, so this test injects a
    DIFFERENT ``ValueError`` — not that subclass — via
    ``estimate_tokens`` (called right after the now-successful
    ``resolver.resolve()`` inside ``TurnBudgetEngine.__init__``) to prove
    it is NOT swallowed."""
    import reyn.services.turn_budget.engine as engine_module

    resolver = ModelResolver(
        {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
        default_class="standard",
    )

    def _boom(*args, **kwargs):
        raise ValueError("an unrelated failure, not an unresolvable model class")

    monkeypatch.setattr(engine_module, "estimate_tokens", _boom)

    with pytest.raises(ValueError, match="an unrelated failure"):
        try_build_default_turn_budget_engine("standard", resolver=resolver)


# ── acceptance② — /model can reach a valid class from a broken initial
# state ────────────────────────────────────────────────────────────────────


def test_switching_from_a_broken_default_to_a_valid_class_builds_a_real_engine() -> None:
    """Tier 2: acceptance② — the exact #4573 recovery path: a resolver
    constructed with an unresolvable ``default_class`` (the broken initial
    state) must still let a caller build a real engine for a VALID class
    (what ``/model <class>`` does via ``_rebuild_derived_model_engines_
    for_model``'s own ``try_build_default_turn_budget_engine`` call) — the
    broken default must not poison resolution of an UNRELATED, valid class
    on the SAME resolver instance."""
    resolver = ModelResolver(
        {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
        default_class="gemini-2.5-flash-lite",
    )
    # the broken default degrades (acceptance④'s own witness) ...
    assert try_build_default_turn_budget_engine(
        "gemini-2.5-flash-lite", resolver=resolver,
    ) is None
    # ... but switching to a real class on the SAME resolver instance works.
    engine = try_build_default_turn_budget_engine("standard", resolver=resolver)
    assert engine is not None


# ── acceptance③ — a genuine turn attempt with the unresolved value still
# fails loudly, naming the value and how to fix it ─────────────────────────


def test_resolve_still_raises_for_a_genuine_use_attempt() -> None:
    """Tier 2: acceptance③ — #3368's own load-bearing raise, UNCHANGED.
    #4573 only moves WHEN the operator first learns about the problem
    (load-time warn) and closes an INCIDENTAL crash site (the lazy budget
    estimate) — it does not touch ``resolve()`` itself, which is what a
    REAL turn-dispatch call (``RouterHostAdapter.resolve_model`` /
    ``RouterLoop``'s own calls) still goes through. A silent degrade here
    would be WORSE than #4573's original bug (per architect's own
    ``起動はするが何も言わない`` (starts but says nothing) is worse) —
    this test is the guard
    against that regression."""
    resolver = ModelResolver(
        {"light": "openai/gpt-4o-mini", "standard": "openai/gpt-4o"},
        default_class="gemini-2.5-flash-lite",
    )
    with pytest.raises(ValueError, match="gemini-2.5-flash-lite"):
        resolver.resolve("gemini-2.5-flash-lite")
