"""Tier 2: OS invariant — 3-layer config cascade (ADR-0031).

Covers:
  - load_config() ignores <project>/.reyn/config.yaml and emits a warning
    when the deprecated file exists.
  - load_config() still loads reyn.local.yaml correctly.
  - _warn_legacy_dot_reyn_config() emits the correct deprecation message.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Tier 2: migration warning — legacy .reyn/config.yaml emits warning, is NOT loaded
# ---------------------------------------------------------------------------


def test_legacy_dot_reyn_config_emits_warning(tmp_path, monkeypatch, capsys):
    """Tier 2: load_config() emits a deprecation warning when .reyn/config.yaml exists.

    The deprecated file must NOT be loaded into the merged config — only
    reyn.local.yaml should be picked up.
    """
    # Create a minimal project root.
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {"model": "standard"})

    # Write a .reyn/config.yaml with a sentinel value.
    legacy_cfg = tmp_path / ".reyn" / "config.yaml"
    _write_yaml(legacy_cfg, {"model": "legacy-only-model"})

    # Write a reyn.local.yaml with a different sentinel.
    local_yaml = tmp_path / "reyn.local.yaml"
    _write_yaml(local_yaml, {"model": "local-model"})

    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config

    cfg = load_config(tmp_path)

    captured = capsys.readouterr()

    # Warning must be emitted.
    assert "deprecated" in captured.err.lower() or "ADR-0031" in captured.err, (
        f"Expected deprecation warning in stderr, got: {captured.err!r}"
    )
    assert ".reyn/config.yaml" in captured.err or str(legacy_cfg) in captured.err

    # Legacy file value must NOT override anything — reyn.local.yaml wins.
    assert cfg.model == "local-model", (
        f"Expected 'local-model' from reyn.local.yaml, got {cfg.model!r}. "
        "The legacy .reyn/config.yaml must not be loaded."
    )


def test_legacy_dot_reyn_config_not_loaded_when_no_local_yaml(tmp_path, monkeypatch, capsys):
    """Tier 2: legacy .reyn/config.yaml is silently ignored (warning emitted, value unused).

    Even when reyn.local.yaml is absent, the deprecated file must not
    override values from reyn.yaml.
    """
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {"model": "project-model"})

    legacy_cfg = tmp_path / ".reyn" / "config.yaml"
    _write_yaml(legacy_cfg, {"model": "legacy-only-model"})

    # No reyn.local.yaml — default from reyn.yaml should survive.
    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config

    cfg = load_config(tmp_path)
    captured = capsys.readouterr()

    # Warning still emitted.
    assert "deprecated" in captured.err.lower() or "ADR-0031" in captured.err

    # Legacy value must not bleed in.
    assert cfg.model == "project-model", (
        f"Expected 'project-model', got {cfg.model!r}. "
        "The legacy .reyn/config.yaml value must not be applied."
    )


def test_no_warning_when_dot_reyn_config_absent(tmp_path, monkeypatch, capsys):
    """Tier 2: no deprecation warning is emitted when .reyn/config.yaml does not exist."""
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {"model": "standard"})

    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config

    load_config(tmp_path)
    captured = capsys.readouterr()

    assert "deprecated" not in captured.err.lower()
    assert ".reyn/config.yaml" not in captured.err


def test_reyn_local_yaml_still_loaded(tmp_path, monkeypatch):
    """Tier 2: reyn.local.yaml is loaded and overrides reyn.yaml (3-layer cascade intact)."""
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {"model": "standard"})

    local_yaml = tmp_path / "reyn.local.yaml"
    _write_yaml(local_yaml, {"model": "strong"})

    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config

    cfg = load_config(tmp_path)
    assert cfg.model == "strong", (
        f"Expected 'strong' from reyn.local.yaml, got {cfg.model!r}."
    )


# ---------------------------------------------------------------------------
# Tier 2: _warn_legacy_dot_reyn_config unit test
# ---------------------------------------------------------------------------


def test_warn_function_prints_to_stderr(tmp_path, capsys):
    """Tier 2: _warn_legacy_dot_reyn_config emits the expected message to stderr."""
    from reyn.config import _warn_legacy_dot_reyn_config

    legacy = tmp_path / ".reyn" / "config.yaml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("model: test\n", encoding="utf-8")

    _warn_legacy_dot_reyn_config(legacy)
    captured = capsys.readouterr()

    assert "ADR-0031" in captured.err
    assert "reyn.local.yaml" in captured.err
    assert "deprecated" in captured.err.lower()
    assert captured.out == ""  # nothing on stdout


def test_warn_function_silent_when_absent(tmp_path, capsys):
    """Tier 2: _warn_legacy_dot_reyn_config is silent when the file does not exist."""
    from reyn.config import _warn_legacy_dot_reyn_config

    _warn_legacy_dot_reyn_config(tmp_path / ".reyn" / "config.yaml")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
