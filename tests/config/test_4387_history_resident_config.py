"""Tier 2: #4387 Phase B ③ — `history_resident:` config parsing.

Mirrors `_build_read_cap_config`'s own tested shape (same discipline: a
missing/malformed value falls back to the default rather than silently
disabling the cap).
"""
from __future__ import annotations

from reyn.config.chat import HistoryResidentConfig, _build_history_resident_config


def test_missing_section_uses_default() -> None:
    """Tier 2: no `history_resident:` key at all -> the shipped default (256 MiB)."""
    cfg = _build_history_resident_config(None)
    assert cfg == HistoryResidentConfig()
    assert cfg.max_bytes == 256 * 1024 * 1024


def test_valid_max_bytes_is_honored() -> None:
    """Tier 2: an explicit, valid value is used verbatim."""
    cfg = _build_history_resident_config({"max_bytes": 12_345})
    assert cfg.max_bytes == 12_345


def test_non_dict_section_uses_default() -> None:
    """Tier 2: `history_resident: "oops"` (wrong shape) -> default, not a crash."""
    cfg = _build_history_resident_config("oops")
    assert cfg.max_bytes == HistoryResidentConfig().max_bytes


def test_zero_or_negative_falls_back_to_default() -> None:
    """Tier 2: an operator typo (0 or negative) must not silently disable the
    cap (0 would evict everything on every append; negative is nonsensical)."""
    assert _build_history_resident_config({"max_bytes": 0}).max_bytes == (
        HistoryResidentConfig().max_bytes
    )
    assert _build_history_resident_config({"max_bytes": -5}).max_bytes == (
        HistoryResidentConfig().max_bytes
    )


def test_non_numeric_falls_back_to_default() -> None:
    """Tier 2: a non-numeric value (e.g. a YAML string) -> default, not a crash."""
    cfg = _build_history_resident_config({"max_bytes": "not-a-number"})
    assert cfg.max_bytes == HistoryResidentConfig().max_bytes


def test_reyn_config_default_reaches_history_resident_field() -> None:
    """Tier 2: wiring sanity — `ReynConfig.history_resident` exists and
    resolves to the same default when no reyn.yaml section is present,
    proving the loader-level `merged.get("history_resident")` -> ReynConfig
    path is actually connected, not just the parser function in isolation."""
    import tempfile
    from pathlib import Path

    from reyn.config.loader import load_config

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "reyn.yaml").write_text(
            "llm:\n  models:\n    standard: fake/x\n", encoding="utf-8",
        )
        cfg = load_config(cwd=root)
    assert cfg.history_resident == HistoryResidentConfig()


def test_reyn_config_honors_an_explicit_history_resident_section() -> None:
    """Tier 2: the real end-to-end path — a `history_resident:` section in a
    real reyn.yaml reaches `ReynConfig.history_resident.max_bytes`."""
    import tempfile
    from pathlib import Path

    from reyn.config.loader import load_config

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "reyn.yaml").write_text(
            "llm:\n  models:\n    standard: fake/x\n"
            "history_resident:\n  max_bytes: 999\n",
            encoding="utf-8",
        )
        cfg = load_config(cwd=root)
    assert cfg.history_resident.max_bytes == 999
