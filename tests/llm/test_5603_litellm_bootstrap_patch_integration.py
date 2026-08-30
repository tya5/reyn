"""Tier 2: #5603 — ``ensure_litellm_ready``'s own integration with reyn's
litellm compat patches: the two DIFFERENT failure semantics architect
ruled for (correctness-critical A propagates uncaught; diagnostic-only B
is caught and warned).

Real ``ensure_litellm_ready``/real litellm import throughout — only the
patch APPLICATION function itself is monkeypatched to simulate "the
private symbol this depends on moved" (a real, environment-shaped
failure this test cannot otherwise force without actually breaking the
installed litellm package), following
``test_litellm_lazy_load.py``'s own established reset pattern for this
module's process-global one-shot state.
"""
from __future__ import annotations

import reyn.llm.litellm_bootstrap as litellm_bootstrap


def _reset_litellm_ready_state():
    """Same reset ``test_litellm_lazy_load.py`` already established for
    this module's process-global one-shot guards, PLUS the #4395 axis②
    cooldown pair (``_litellm_import_cooldown_until``/``_litellm_import_
    failure_warned``) — a forced failure in one test would otherwise arm
    the cooldown and make every subsequent test's own genuine attempt
    silently short-circuit to ``None`` without even importing, exactly
    the bug this reset must not reproduce. Returns a restore callable."""
    saved_ready = litellm_bootstrap._litellm_ready
    saved_registry = dict(litellm_bootstrap._ready_registry)
    saved_cooldown = litellm_bootstrap._litellm_import_cooldown_until
    saved_warned = litellm_bootstrap._litellm_import_failure_warned
    litellm_bootstrap._litellm_ready = False
    litellm_bootstrap._ready_registry.clear()
    litellm_bootstrap._litellm_import_cooldown_until = 0.0
    litellm_bootstrap._litellm_import_failure_warned = False

    def _restore() -> None:
        litellm_bootstrap._litellm_ready = saved_ready
        litellm_bootstrap._ready_registry.clear()
        litellm_bootstrap._ready_registry.update(saved_registry)
        litellm_bootstrap._litellm_import_cooldown_until = saved_cooldown
        litellm_bootstrap._litellm_import_failure_warned = saved_warned

    return _restore


def test_correctness_critical_patch_failure_makes_litellm_unusable(monkeypatch) -> None:
    """Tier 2: #5603 accept — when the correctness-critical patch (A)
    cannot be applied (its own private symbols moved), the SAME
    `except Exception: result = None` branch a real `import litellm`
    failure already uses catches it — `ensure_litellm_ready()` returns
    `None`, exactly the "litellm unusable this call" signal every no-
    fallback caller already knows how to handle. Never a silent "started
    anyway with a known-broken bridge"."""
    import reyn.llm._litellm_compat_patches as patches

    def _broken_apply_a(events=None):
        raise AttributeError("reyn #5603 test: simulated moved private symbol")

    monkeypatch.setattr(patches, "apply_stream_chunk_recovery", _broken_apply_a)

    restore = _reset_litellm_ready_state()
    try:
        result = litellm_bootstrap.ensure_litellm_ready()
        assert result is None, (
            "a correctness-critical patch-apply failure must make "
            "ensure_litellm_ready() return None, same as any other "
            "import-litellm failure — got a real module instead"
        )
    finally:
        restore()


def test_diagnostic_only_patch_failure_still_leaves_litellm_usable(monkeypatch) -> None:
    """Tier 2: #5603 deny — when the diagnostic-only patch (B) cannot be
    applied, litellm is STILL returned as usable (its own failure is
    caught and warned inside ``apply_all``, never propagated) — a worse
    diagnosis on a call that would have failed anyway is not the same
    class of defect as a wrong DATA result on an otherwise-successful
    call."""
    import reyn.llm._litellm_compat_patches as patches

    def _broken_apply_b(events=None):
        raise AttributeError("reyn #5603 test: simulated moved private symbol")

    monkeypatch.setattr(patches, "apply_overflow_diagnosis", _broken_apply_b)

    restore = _reset_litellm_ready_state()
    try:
        result = litellm_bootstrap.ensure_litellm_ready()
        assert result is not None, (
            "a diagnostic-only patch-apply failure must NOT make "
            "ensure_litellm_ready() return None — litellm itself is "
            "still perfectly usable"
        )
    finally:
        restore()


def test_both_patches_applying_cleanly_leaves_litellm_usable() -> None:
    """Tier 2: #5603 accept (sanity) — the ordinary, unbroken case."""
    restore = _reset_litellm_ready_state()
    try:
        result = litellm_bootstrap.ensure_litellm_ready()
        assert result is not None
    finally:
        restore()
