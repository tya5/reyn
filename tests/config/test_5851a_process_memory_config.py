"""Tier 2: #5851 stage (a) — `process_memory:` config parsing + the
enforce-at-load validation (architect ruling ②).

Mirrors `test_4387_history_resident_config.py`'s own tested shape where the
discipline is the SAME (non-dict/malformed section -> a safe fallback), and
deliberately diverges where the architect ruling says the two axes differ:
`max_bytes` has no default NUMBER to fall back to (`None` = no cap IS the
shipped default), so a malformed value falls back to `None`, never to some
other magic number.
"""
from __future__ import annotations

import pytest

from reyn.config.chat import ProcessMemoryConfig, _build_process_memory_config
from reyn.config.loader import _validate_process_memory
from reyn.config.root import ReynConfig


def test_missing_section_is_observe_only_by_default() -> None:
    """Tier 2: no `process_memory:` key at all -> no cap, enforce off — the
    shipped stage (a) default (architect ruling: "既定 enforce: false・
    max_bytes 無し")."""
    cfg = _build_process_memory_config(None)
    assert cfg == ProcessMemoryConfig()
    assert cfg.max_bytes is None
    assert cfg.enforce is False


def test_valid_max_bytes_and_enforce_are_honored() -> None:
    """Tier 2: explicit valid values are used verbatim."""
    cfg = _build_process_memory_config({"max_bytes": 2_000_000_000, "enforce": True})
    assert cfg.max_bytes == 2_000_000_000
    assert cfg.enforce is True


def test_non_dict_section_is_observe_only() -> None:
    """Tier 2: `process_memory: "oops"` (wrong shape) -> the safe default, not
    a crash."""
    cfg = _build_process_memory_config("oops")
    assert cfg == ProcessMemoryConfig()


def test_zero_or_negative_max_bytes_falls_back_to_none_not_zero() -> None:
    """Tier 2: architect ruling — `max_bytes: 0` must NOT mean "disabled";
    a non-positive value is treated as ABSENT (falls back to `None`, the
    same "no cap" state omitting the key produces), never coerced to the
    literal number 0 (which `dispatch_tool`-style axes elsewhere in this
    repo would misread as a real, zero-byte cap)."""
    assert _build_process_memory_config({"max_bytes": 0}).max_bytes is None
    assert _build_process_memory_config({"max_bytes": -5}).max_bytes is None


def test_non_numeric_max_bytes_falls_back_to_none() -> None:
    """Tier 2: a non-numeric value (e.g. a YAML string) -> None, not a crash."""
    cfg = _build_process_memory_config({"max_bytes": "not-a-number"})
    assert cfg.max_bytes is None


def test_enforce_missing_defaults_false() -> None:
    """Tier 2: `max_bytes` alone, no `enforce:` key -> observe-only (the two
    keys are independent facts — setting a cap does not itself turn on
    halting, architect ruling ②'s own "2 事実 2 key")."""
    cfg = _build_process_memory_config({"max_bytes": 500})
    assert cfg.max_bytes == 500
    assert cfg.enforce is False


def test_reyn_config_default_reaches_process_memory_field() -> None:
    """Tier 2: wiring sanity — `ReynConfig.process_memory` exists and
    resolves to the shipped observe-only default with no reyn.yaml
    section present (mirrors history_resident's own equivalent test)."""
    assert ReynConfig().process_memory == ProcessMemoryConfig()


# ── enforce-at-load validation (architect ruling ②, loader.py) ─────────────


def test_enforce_true_without_max_bytes_raises_at_load() -> None:
    """Tier 2: `enforce: true` with no `max_bytes` has nothing to compare
    the measured footprint against — a LOAD-time error (never a silent
    no-op), mirroring `_validate_retrieval_scheme_embedding`'s own
    enforce-at-load precedent in this same module."""
    cfg = ReynConfig(process_memory=ProcessMemoryConfig(max_bytes=None, enforce=True))
    with pytest.raises(ValueError, match="process_memory.max_bytes"):
        _validate_process_memory(cfg)


def test_enforce_true_on_a_readerless_platform_raises_at_load(monkeypatch) -> None:
    """Tier 2: `enforce: true` with a real `max_bytes`, but on a platform
    with no reader (`process_memory_metric_name()` returns None) — the
    cap could never be checked, so this is ALSO a load-time error, not a
    quietly-inert `enforce: true`. Falsify note: strip this branch (or the
    monkeypatch below) and the call succeeds instead of raising."""
    import reyn.runtime.process_memory as process_memory_module

    monkeypatch.setattr(
        process_memory_module, "process_memory_metric_name", lambda: None,
    )
    cfg = ReynConfig(
        process_memory=ProcessMemoryConfig(max_bytes=1_000_000, enforce=True),
    )
    with pytest.raises(ValueError, match="no process-memory reader"):
        _validate_process_memory(cfg)


def test_enforce_true_with_max_bytes_on_a_real_reader_platform_does_not_raise() -> None:
    """Tier 2: the positive control for the two error tests above — a
    fully-specified, checkable config does NOT raise."""
    cfg = ReynConfig(
        process_memory=ProcessMemoryConfig(max_bytes=1_000_000, enforce=True),
    )
    _validate_process_memory(cfg)  # must not raise


def test_enforce_false_never_raises_regardless_of_max_bytes() -> None:
    """Tier 2: observe-only (`enforce: false`) never triggers the load
    error, even with no `max_bytes` at all — the validation is only about
    whether a STATED intent to halt is checkable, not about the presence
    of a cap by itself."""
    _validate_process_memory(ReynConfig())  # must not raise
