"""Tier 1: #5366 — `storage:` config parsing (the project-wide,
cross-session history-content disk cap).

`max_bytes` — a non-positive or non-numeric value falls back to the
field's own default (``None`` — unlimited), the SAME state an operator
who wrote nothing gets. Unlike #4601's `remote_fallback_limit` (whose
default IS the active cap, so falling back never disables anything),
this field's own default already means "the cap is off" by design
(architect's ruling), so a malformed value is not a silent widening of
an otherwise-engaged cap — it lands on the field's own steady state.
"""
from __future__ import annotations

from reyn.config.infra import StorageConfig, _build_storage_config


def test_default_is_unlimited_with_no_pins():
    """Tier 1: no `storage:` block at all — unlimited (None), no pins.
    Architect's ruling: "off" is represented by max_bytes' own absence,
    not a separate boolean."""
    cfg = _build_storage_config(None)
    assert cfg.max_bytes is None
    assert cfg.pin == []


def test_positive_max_bytes_passes_through():
    """Tier 1: an ordinary operator override parses through unmodified."""
    cfg = _build_storage_config({"max_bytes": 5_000_000})
    assert cfg.max_bytes == 5_000_000


def test_zero_falls_back_to_unlimited():
    """Tier 1: 0 is not a meaningful cap (nothing could ever fit) —
    falls back to the field's own default (None) rather than producing
    a cap that always fires."""
    cfg = _build_storage_config({"max_bytes": 0})
    assert cfg.max_bytes is None


def test_negative_value_falls_back_to_unlimited():
    """Tier 1: a negative cap doesn't mean anything sensible."""
    cfg = _build_storage_config({"max_bytes": -5})
    assert cfg.max_bytes is None


def test_non_numeric_value_falls_back_to_unlimited():
    """Tier 1: (accept-side) a malformed/non-numeric value falls back
    rather than raising or propagating a string into later int math."""
    cfg = _build_storage_config({"max_bytes": "not-a-number"})
    assert cfg.max_bytes is None


def test_explicit_null_max_bytes_is_unlimited():
    """Tier 1: an operator writing `max_bytes: null` (YAML None) gets the
    same unlimited state as omitting the key entirely."""
    cfg = _build_storage_config({"max_bytes": None})
    assert cfg.max_bytes is None


def test_pin_list_of_agent_names_passes_through():
    """Tier 1: pin names AGENT names (not session ids — see StorageConfig's
    own docstring for why), parsed as a plain list of strings."""
    cfg = _build_storage_config({"pin": ["coder-smith", "coder-brown"]})
    assert cfg.pin == ["coder-smith", "coder-brown"]


def test_pin_filters_non_string_entries():
    """Tier 1: (accept-side) a malformed pin list entry (not a string)
    is dropped rather than propagating a non-agent-name value into a
    later path-name comparison."""
    cfg = _build_storage_config({"pin": ["coder-smith", 42, None, "coder-brown"]})
    assert cfg.pin == ["coder-smith", "coder-brown"]


def test_non_list_pin_falls_back_to_empty():
    """Tier 1: (accept-side) `pin` written as something other than a
    list (e.g. a bare string) falls back to no pins, rather than
    iterating a string's own characters as "agent names"."""
    cfg = _build_storage_config({"pin": "coder-smith"})
    assert cfg.pin == []


def test_non_dict_raw_returns_defaults():
    """Tier 1: (accept-side) no `storage:` block at all in reyn.yaml —
    `raw` is `None` (or any non-dict) — returns the plain defaults, not
    an error."""
    assert _build_storage_config(None) == StorageConfig()
    assert _build_storage_config("not-a-dict") == StorageConfig()
