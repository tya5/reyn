"""Tier 2: FP-0043 Phase 4 — default ``embedding_class`` flip to ``local-mini``
+ ChatSession graceful-degrade probe when the ``local-embed`` extras
aren't installed.

What lands here:
  1. ``ActionRetrievalConfig`` default is ``"local-mini"`` (= flipped
     from None in this PR).
  2. ``is_available()`` is a cheap importlib.util.find_spec probe and
     reflects the live env (= True iff sentence_transformers is
     importable).
  3. ``_embedding_class_needs_missing_extras`` correctly identifies
     when the configured class would require the missing extras:

     - ST-backed class + extras absent  → True  (graceful degrade)
     - ST-backed class + extras present → False (wire normally)
     - OpenAI / LiteLLM class           → False (no extras dependency)
     - Unknown class name               → False (let normal path raise)
     - Malformed embedding_config       → False (defensive)

  4. The end-to-end shape: when the probe returns True the session
     skips embedding-index wiring (= same outcome as
     ``embedding_class=None``).

Phase 4 design intent: fresh users who install ``reyn[local-embed]``
get ``search_actions`` automatically (= no reyn.yaml edits). Fresh
users WITHOUT extras see the existing hidden-state hint via
``list_actions`` pointing them at the install command. No scary
ImportError at first embed() call; graceful from session start.
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from reyn.chat.session import _embedding_class_needs_missing_extras
from reyn.config import (
    ActionRetrievalConfig,
    EmbeddingClassSpec,
    EmbeddingConfig,
)
from reyn.embedding.sentence_transformers_provider import is_available

# ── 1. Default config value ────────────────────────────────────────────────


def test_default_embedding_class_is_local_mini() -> None:
    """Tier 2: out-of-the-box ``ActionRetrievalConfig`` selects local-mini.

    Phase 4 design: zero-config fresh users with ``reyn[local-embed]``
    installed get semantic search active without touching reyn.yaml.
    """
    cfg = ActionRetrievalConfig()
    assert cfg.embedding_class == "local-mini"


# ── 2. is_available() probe ────────────────────────────────────────────────


def test_is_available_matches_live_import_spec() -> None:
    """Tier 2: is_available() agrees with importlib.util.find_spec.

    The probe doesn't import the heavy module — only checks the spec.
    Whichever way the env is configured (= extras installed or not),
    both checks must produce the same boolean.
    """
    live_check = importlib.util.find_spec("sentence_transformers") is not None
    assert is_available() is live_check


# ── 3. Probe — ST class, extras-dependent matrix ───────────────────────────


def _config_with_classes(**extra: EmbeddingClassSpec) -> EmbeddingConfig:
    base: dict[str, EmbeddingClassSpec] = {
        "local-mini": EmbeddingClassSpec(
            model="sentence-transformers/all-MiniLM-L6-v2",
        ),
        "local-e5": EmbeddingClassSpec(
            model="sentence-transformers/intfloat/multilingual-e5-small",
        ),
        "standard": EmbeddingClassSpec(
            model="openai/text-embedding-3-small",
        ),
    }
    base.update(extra)
    return EmbeddingConfig(default_class="standard", classes=base)


def test_probe_st_class_when_extras_missing_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ST class + import unavailable → True (graceful degrade).

    Simulates a fresh user who set ``embedding_class: local-mini`` (or
    inherited the Phase 4 default) but never ran
    ``pip install 'reyn[local-embed]'``. The probe must return True so
    ChatSession skips embedding wiring entirely; the hidden-state hint
    path on ``list_actions`` takes over the user-discovery surface.
    """
    monkeypatch.setattr(
        "reyn.embedding.sentence_transformers_provider.is_available",
        lambda: False,
    )
    cfg = _config_with_classes()
    assert _embedding_class_needs_missing_extras("local-mini", cfg) is True
    assert _embedding_class_needs_missing_extras("local-e5", cfg) is True


def test_probe_st_class_when_extras_present_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: ST class + import available → False (wire normally).

    User installed ``reyn[local-embed]``. Probe says "no degrade
    needed"; the normal wiring path builds the EmbeddingProvider +
    ActionEmbeddingIndex and ``search_actions`` becomes visible after
    the first index build completes.
    """
    monkeypatch.setattr(
        "reyn.embedding.sentence_transformers_provider.is_available",
        lambda: True,
    )
    cfg = _config_with_classes()
    assert _embedding_class_needs_missing_extras("local-mini", cfg) is False
    assert _embedding_class_needs_missing_extras("local-e5", cfg) is False


# ── 4. Probe — non-ST classes never trigger graceful degrade ───────────────


@pytest.mark.parametrize("st_available", [True, False])
def test_probe_openai_class_returns_false(
    monkeypatch: pytest.MonkeyPatch, st_available: bool,
) -> None:
    """Tier 2: OpenAI-backed class doesn't depend on ST extras.

    Regardless of whether ``sentence_transformers`` is importable,
    setting ``embedding_class: standard`` (= openai/...) must NOT
    trigger graceful-degrade. The LiteLLM backend handles it; the
    probe is irrelevant to that path.
    """
    monkeypatch.setattr(
        "reyn.embedding.sentence_transformers_provider.is_available",
        lambda: st_available,
    )
    cfg = _config_with_classes()
    assert _embedding_class_needs_missing_extras("standard", cfg) is False


# ── 5. Probe — defensive cases ─────────────────────────────────────────────


def test_probe_unknown_class_returns_false() -> None:
    """Tier 2: unknown class name → False (let downstream raise normally).

    The probe is a fast filter, not a config validator. Unknown
    classes flow through to the normal try/except wiring path where
    KeyError surfaces as a Session-init failure via the existing
    catch-all (= consistent with Pre-Phase-4 behavior).
    """
    cfg = _config_with_classes()
    assert _embedding_class_needs_missing_extras("nope", cfg) is False


def test_probe_malformed_config_returns_false() -> None:
    """Tier 2: malformed embedding_config (= missing .classes) → False.

    Defensive against exotic test fixtures or partial config shapes.
    Returning False lets the existing try/except handle the surface
    rather than the probe masking real config errors.
    """
    malformed = SimpleNamespace()  # no .classes attribute
    assert _embedding_class_needs_missing_extras("local-mini", malformed) is False


def test_probe_non_string_model_field_returns_false() -> None:
    """Tier 2: spec with non-string model → False (defensive)."""
    cfg = EmbeddingConfig(
        default_class="standard",
        classes={"weird": SimpleNamespace(model=None)},  # type: ignore[dict-item]
    )
    assert _embedding_class_needs_missing_extras("weird", cfg) is False


def test_probe_st_prefix_substring_doesnt_trigger() -> None:
    """Tier 2: model name that merely contains the prefix does NOT trigger.

    Only the leading ``sentence-transformers/`` qualifies. A model
    string like ``foo/sentence-transformers/...`` is treated as
    not-ST (= future-compat shield).
    """
    cfg = EmbeddingConfig(
        default_class="standard",
        classes={
            "tricky": EmbeddingClassSpec(model="foo/sentence-transformers/bar"),
        },
    )
    assert _embedding_class_needs_missing_extras("tricky", cfg) is False


# ── 6. End-to-end shape (= the probe is wired into the gate) ──────────────


def test_probe_is_referenced_in_session_init_source() -> None:
    """Tier 2: the probe call appears in ChatSession.__init__'s gate.

    We don't construct a full ChatSession here (= heavy deps). Instead
    we sanity-check that ``_embedding_class_needs_missing_extras`` is
    actually invoked from the gate by inspecting the module source.
    Catches a future accidental removal that would silently disable
    the graceful-degrade branch (= the existing default-flip would
    then start raising ImportError at first embed() for fresh users
    without extras).
    """
    import inspect

    import reyn.chat.session as session_mod
    src = inspect.getsource(session_mod.ChatSession.__init__)
    assert "_embedding_class_needs_missing_extras" in src
