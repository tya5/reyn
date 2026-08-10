"""Tier 2: safety.threat_scan config round-trip (FP-0050 / #1822 S1).

The config is the operator surface for the content-threat defense. Round-trip
uses NON-default values (per the #302 lesson — a default round-trip passes
trivially for an unwired field).

Falsification: each assertion reads a non-default value back through the full
``reyn.yaml`` → ``load_config`` → ``SafetyConfig.threat_scan`` path; if the
field were unwired (not parsed in ``_build_safety_config`` or not deep-merged in
the loader) the value would fall back to the default and the assertion fail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import ThreatScanConfig, load_config


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_threat_scan_defaults_when_absent(isolated: Path):
    """Tier 2: no safety.threat_scan section → dataclass defaults."""
    (isolated / "reyn.yaml").write_text("agent:\n  name: t\n", encoding="utf-8")
    cfg = load_config(cwd=isolated)
    assert cfg.safety.threat_scan == ThreatScanConfig()


def test_threat_scan_nondefault_round_trip(isolated: Path):
    """Tier 2: non-default safety.threat_scan.* flows through to the config."""
    (isolated / "reyn.yaml").write_text(
        "safety:\n"
        "  threat_scan:\n"
        "    enabled: false\n"
        "    fail_open: false\n"
        "    fence_enabled: false\n"
        "    block_severity: warn\n"
        "    custom_patterns:\n"
        '      - ["\\\\bxyzzy\\\\b", "custom_xyzzy", "context", "block"]\n',
        encoding="utf-8",
    )
    cfg = load_config(cwd=isolated)
    ts = cfg.safety.threat_scan

    assert ts.enabled is False
    assert ts.fail_open is False
    assert ts.fence_enabled is False
    assert ts.block_severity == "warn"
    assert any(p[1] == "custom_xyzzy" for p in ts.custom_patterns)
