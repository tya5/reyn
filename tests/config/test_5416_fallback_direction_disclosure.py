"""Tier 1: #5416 — known-key/malformed-value FAIL-OPEN fallbacks become
operator-visible, folded into the SAME combined report
``config_schema.unknown_config_keys()`` already populates for unknown
keys — not a second parallel channel (architect's ruling).

``StorageConfig.pin`` is the confirmed instance (lead-coder BLOCKING on
#5415): a malformed ``pin`` value falls back to ``[]``, silently
REMOVING a declared eviction protection — unlike ``max_bytes`` (whose
fallback to ``None``/unlimited is the SAME state an operator who wrote
nothing gets, harmless). The two directions are declared on the field
itself (``metadata={"fallback": FAILS_SAFE | FAILS_OPEN}``) — a runtime
registry (``dataclasses.fields(StorageConfig)``) a future field cannot
silently omit, unlike a parser-side census.
"""
from __future__ import annotations

import dataclasses

from reyn.config.config_schema import FAILS_OPEN, FAILS_SAFE, RejectedValueHint
from reyn.config.infra import StorageConfig, _build_storage_config

# ── field-level declaration (the completeness gate, architect's #6/#6b) ────


def test_every_storage_config_field_declares_a_fallback_direction():
    """Tier 1: architect's completeness gate #6 — every field of
    StorageConfig (the population this issue's instance concerns; the
    broader 28+ site census across src/reyn/config/ is explicitly
    out of scope per lead-coder's own disclaimer on #5416) declares
    ``metadata["fallback"]``. A future field added to this dataclass
    without the declaration fails here, not silently."""
    fields = dataclasses.fields(StorageConfig)
    for f in fields:
        assert "fallback" in f.metadata, (
            f"StorageConfig.{f.name} has no metadata['fallback'] "
            f"declaration — #5416 requires every field to name whether "
            f"its own fallback is fails_safe or fails_open"
        )
        assert f.metadata["fallback"] in (FAILS_SAFE, FAILS_OPEN)


def test_the_completeness_gate_is_not_vacuous():
    """Tier 1: architect's #6b — the empty-collection guard. Without
    this, a typo'd class name or import in the test above would make
    ``dataclasses.fields(...)`` return an empty tuple, and "every field
    declares a fallback" would be vacuously true over zero fields."""
    assert len(dataclasses.fields(StorageConfig)) > 0


def test_max_bytes_declares_fails_safe():
    """Tier 1: the specific direction for max_bytes — its fallback lands
    in the same state an operator who wrote nothing gets."""
    (field,) = [f for f in dataclasses.fields(StorageConfig) if f.name == "max_bytes"]
    assert field.metadata["fallback"] == FAILS_SAFE


def test_pin_declares_fails_open():
    """Tier 1: the specific direction for pin — its fallback REMOVES a
    declared protection, more permissive than unset."""
    (field,) = [f for f in dataclasses.fields(StorageConfig) if f.name == "pin"]
    assert field.metadata["fallback"] == FAILS_OPEN


# ── parser-side reporting ───────────────────────────────────────────────


def test_malformed_pin_top_level_type_is_reported():
    """Tier 1: #5416's own witness — the exact case the issue was filed
    over (`pin: coder-smith`, a bare string instead of a list)."""
    rejected: dict = {}
    cfg = _build_storage_config({"pin": "coder-smith"}, rejected=rejected)

    assert cfg.pin == []
    assert "storage.pin" in rejected
    hint = rejected["storage.pin"]
    assert isinstance(hint, RejectedValueHint)
    assert hint.fallback == FAILS_OPEN
    assert "coder-smith" in hint.note


def test_pin_with_a_non_string_entry_is_reported():
    """Tier 1: a partially-malformed list (one bad entry silently
    dropped) is reported too, not just a wholesale wrong-type value —
    the same class of harm (a declared pin silently not applying)."""
    rejected: dict = {}
    cfg = _build_storage_config({"pin": ["alice", 123]}, rejected=rejected)

    assert cfg.pin == ["alice"]
    assert "storage.pin" in rejected


def test_absent_pin_is_not_reported():
    """Tier 1: (accept-side) control — a key that was never written at
    all is not malformed, just unset. Must not be reported (that would
    make every operator who never touched `storage:` see a spurious
    warning)."""
    rejected: dict = {}
    _build_storage_config({}, rejected=rejected)
    assert rejected == {}


def test_valid_pin_is_not_reported():
    """Tier 1: (accept-side) control — a well-formed pin list produces
    no report at all."""
    rejected: dict = {}
    _build_storage_config({"pin": ["alice", "bob"]}, rejected=rejected)
    assert rejected == {}


def test_malformed_max_bytes_is_never_reported_fails_safe():
    """Tier 1: the direction control — max_bytes is FAILS_SAFE, so even
    an egregiously malformed value must NOT appear in *rejected* (its
    fallback lands in the same state as unset — nothing to warn about).
    Without this control, a bug that reports EVERY malformed value
    regardless of direction would pass every FAILS_OPEN test above for
    the wrong reason."""
    rejected: dict = {}
    cfg = _build_storage_config(
        {"max_bytes": "not-a-number", "pin": ["alice"]}, rejected=rejected,
    )
    assert cfg.max_bytes is None
    assert "storage.max_bytes" not in rejected
    assert rejected == {}


def test_rejected_is_optional_backward_compatible():
    """Tier 1: non-regression — every existing call site that does not
    pass `rejected=` at all must keep working unchanged (the parameter
    is keyword-only with a None default)."""
    cfg = _build_storage_config({"pin": "not-a-list"})
    assert cfg.pin == []


# ── end-to-end wiring, through the real load_config() seam ─────────────


def test_malformed_pin_reaches_reynconfig_unknown_config_keys(tmp_path):
    """Tier 2: #5416's own wiring witness — driven through the REAL
    `load_config(cwd)` seam (the same one #4174 T0's own tests use for
    unknown keys), not `_build_storage_config` called in isolation. A
    malformed `storage.pin` must appear in `ReynConfig.unknown_config_
    keys` (the SAME combined report the "N config keys not applied"
    chrome reads) and be counted in `unknown_config_key_count` — the
    architect's #5416 ruling that this is folded into the existing
    surface, not a second parallel channel."""
    (tmp_path / "reyn.yaml").write_text(
        "storage:\n  pin: coder-smith\n", encoding="utf-8",
    )
    from reyn.config import load_config

    cfg = load_config(tmp_path)

    assert "storage.pin" in cfg.unknown_config_keys
    assert cfg.unknown_config_key_count >= 1


def test_well_formed_storage_config_reaches_reynconfig_cleanly(tmp_path):
    """Tier 2: (accept-side) control for the wiring test above — a
    well-formed `storage:` block produces NO entry, through the same
    real seam."""
    (tmp_path / "reyn.yaml").write_text(
        "storage:\n  max_bytes: 1000000\n  pin:\n    - alice\n",
        encoding="utf-8",
    )
    from reyn.config import load_config

    cfg = load_config(tmp_path)

    assert "storage.pin" not in cfg.unknown_config_keys
    assert "storage.max_bytes" not in cfg.unknown_config_keys
