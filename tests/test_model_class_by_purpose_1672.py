"""Tier 2: #1672 — per-purpose model class is user-configurable (not hardcoded).

The purpose→tier mapping (router="light", control_ir/tool="standard") was
hardcoded in code, so the user could set what a class resolves to but NOT which
class each purpose uses. This adds `model_class_by_purpose` (reyn.yaml) +
`ReynConfig.model_class_for` / `ModelResolver.class_for_purpose` /
`resolve_purpose_class`, with the owner-confirmed default: an UNSET purpose
follows the configured `model` (no hidden cheaper tier); `light` is an explicit
opt-in. Explicit per-call selections still win.

No mocks: real `ReynConfig` / `ModelResolver` instances + `load_config` round-trip.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import ReynConfig, load_config
from reyn.llm.model_resolver import ModelResolver, resolve_purpose_class

# ── ReynConfig.model_class_for ─────────────────────────────────────────────────


def test_config_unset_purpose_follows_model() -> None:
    """Tier 2: #1672 — an unset purpose falls back to `model` (the configured
    main) — the owner's default: routing follows the configured model."""
    cfg = ReynConfig(model="strong", model_class_by_purpose={})
    assert cfg.model_class_for("router") == "strong"
    assert cfg.model_class_for("control_ir") == "strong"


def test_config_override_wins() -> None:
    """Tier 2: #1672 — a per-purpose override wins over `model` (the explicit
    cheap-router opt-in)."""
    cfg = ReynConfig(model="strong", model_class_by_purpose={"router": "light"})
    assert cfg.model_class_for("router") == "light"
    # other purposes still follow `model`
    assert cfg.model_class_for("judge") == "strong"


# ── ModelResolver.class_for_purpose ────────────────────────────────────────────


def test_resolver_class_for_purpose_override_and_default() -> None:
    """Tier 2: #1672 — ModelResolver.class_for_purpose: override wins, unset →
    the configured default class."""
    r = ModelResolver({}, default_class="strong", purpose_classes={"router": "light"})
    assert r.class_for_purpose("router") == "light"
    assert r.class_for_purpose("control_ir") == "strong"


def test_resolver_default_class_is_standard_at_default() -> None:
    """Tier 2: #1672 — a ModelResolver built WITHOUT config (the test / no-config
    fallback) defaults to "standard" for every purpose — identical to the former
    hardcodes (CAT-2/3/4 unchanged at the default config)."""
    r = ModelResolver({})
    assert r.class_for_purpose("router") == "standard"
    assert r.class_for_purpose("control_ir") == "standard"
    assert r.class_for_purpose("judge") == "standard"


# ── resolve_purpose_class (shared by RouterLoop + planner) ──────────────────────


def test_resolve_purpose_class_explicit_wins() -> None:
    """Tier 2: #1672 — an explicit (caller-supplied) value wins over the resolver."""
    r = ModelResolver({}, default_class="strong")
    assert resolve_purpose_class("light", r, "router") == "light"


def test_resolve_purpose_class_unset_uses_resolver() -> None:
    """Tier 2: #1672 — None → the resolver's per-purpose class (the router/plan
    resolution point; this is what flips the chat router from the old hidden
    "light" to follow-config)."""
    r = ModelResolver({}, default_class="strong", purpose_classes={"router": "light"})
    assert resolve_purpose_class(None, r, "router") == "light"
    assert resolve_purpose_class(None, r, "control_ir") == "strong"


def test_resolve_purpose_class_no_resolver_is_standard() -> None:
    """Tier 2: #1672 — None + no resolver (host stub) → "standard" (safe default,
    byte-identical to the old fallback)."""
    assert resolve_purpose_class(None, None, "router") == "standard"


# ── yaml → ReynConfig round-trip (non-default value pins the parser) ────────────


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_yaml_round_trip_non_default(tmp_path, monkeypatch) -> None:
    """Tier 2: #1672 — model_class_by_purpose parses from reyn.yaml (a NON-default
    value pins the parser wiring, not a trivial empty round-trip)."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "reyn.yaml", """
model: strong
models:
  strong: gemini/gemini-2.5-pro
  light: openai/gpt-4o-mini
model_class_by_purpose:
  router: light
  control_ir: strong
""".lstrip())
    cfg = load_config(cwd=tmp_path)
    assert cfg.model_class_by_purpose == {"router": "light", "control_ir": "strong"}
    # And the helper resolves through it.
    assert cfg.model_class_for("router") == "light"
    assert cfg.model_class_for("compaction") == "strong"  # unset → model


def test_yaml_unknown_purpose_warns_but_loads(tmp_path, monkeypatch, caplog) -> None:
    """Tier 2: #1672 — an unknown purpose key warns (decision-enabling, catches a
    typo) but does not crash load_config (forward-compatible)."""
    import logging

    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "reyn.yaml", """
model: standard
model_class_by_purpose:
  rooter: light
""".lstrip())
    with caplog.at_level(logging.WARNING):
        cfg = load_config(cwd=tmp_path)
    assert "rooter" in cfg.model_class_by_purpose  # preserved (not dropped)
    assert any("rooter" in r.getMessage() and "purpose" in r.getMessage()
               for r in caplog.records), "expected a typo warning naming the bad key"
