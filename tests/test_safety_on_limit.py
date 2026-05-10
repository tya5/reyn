"""Tier 2 invariants for FP-0005 Phase 1 — ``safety.on_limit:`` config +
``RunResult.partial_data``.

Phase 1 ships the architectural foundation: the operator can configure
the ``mode`` (interactive / unattended / auto_extend), and ``RunResult``
exposes ``partial_data`` so callers can render "here's what we have so
far" UX after any limit-driven abort. The per-site ``ask_user``
integration (= router_cap, max_phase_visits, phase_seconds, chain_seconds,
max_hop_depth, max_act_turns) is Phase 2 of FP-0005 — at Phase 1 the
mode is parsed and exposed via ``ReynConfig.safety.on_limit`` but
defaults to ``unattended`` so legacy abort behaviour is preserved.

These tests pin:
  1. ``OnLimitConfig`` dataclass defaults match the legacy abort path
     (= ``unattended``, 1 auto-extend, 60s ask timeout).
  2. ``safety.on_limit:`` keys parse correctly, including the
     ``mode`` enum validation (unknown values fall back to default).
  3. ``RunResult.partial_data`` is None on a clean ``finished`` run,
     populated on abort paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    ON_LIMIT_MODES,
    OnLimitConfig,
    SafetyConfig,
    load_config,
)


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# ─── 1. OnLimitConfig defaults ─────────────────────────────────────────


def test_on_limit_default_is_unattended() -> None:
    """Tier 2: the default ``OnLimitConfig`` mode is ``unattended`` so
    legacy callers (= every existing reyn run / chat without explicit
    config) preserve their abort-on-limit behaviour. Opt into
    ``interactive`` / ``auto_extend`` is explicit.
    """
    cfg = OnLimitConfig()
    assert cfg.mode == "unattended"
    assert cfg.auto_extend_times == 1
    assert cfg.ask_timeout_seconds == 60.0


def test_on_limit_modes_constant_includes_all_three() -> None:
    """Tier 2: ``ON_LIMIT_MODES`` is the exhaustive list of valid modes
    used by the parser for enum validation.
    """
    assert ON_LIMIT_MODES == ("interactive", "unattended", "auto_extend")


# ─── 2. Loader parses safety.on_limit ─────────────────────────────────


def test_safety_on_limit_keys_parse(isolated_project: Path) -> None:
    """Tier 2: ``safety.on_limit:`` keys flow into ``OnLimitConfig``
    fields. All three knobs (mode / auto_extend_times /
    ask_timeout_seconds) round-trip through YAML.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  on_limit:
    mode: interactive
    auto_extend_times: 3
    ask_timeout_seconds: 120
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.on_limit.mode == "interactive"
    assert cfg.safety.on_limit.auto_extend_times == 3
    assert cfg.safety.on_limit.ask_timeout_seconds == 120.0


def test_safety_on_limit_unknown_mode_falls_back(
    isolated_project: Path,
) -> None:
    """Tier 2: when ``mode`` is not one of the three valid values, the
    loader logs a warning and falls back to the default. Config-level
    typos must NEVER block startup (= same convention as
    ``skill_resume.default``).
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  on_limit:
    mode: not-a-real-mode
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.on_limit.mode == "unattended"


def test_safety_on_limit_negative_values_clamped(
    isolated_project: Path,
) -> None:
    """Tier 2: negative ``auto_extend_times`` / ``ask_timeout_seconds``
    fall back to defaults. We never silently accept a negative window
    that would short-circuit the ask loop.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  on_limit:
    mode: interactive
    auto_extend_times: -5
    ask_timeout_seconds: -1
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.on_limit.auto_extend_times == 1
    assert cfg.safety.on_limit.ask_timeout_seconds == 60.0


def test_safety_section_default_includes_unattended_on_limit(
    isolated_project: Path,
) -> None:
    """Tier 2: an empty ``safety:`` (or no safety: at all) yields a
    ``SafetyConfig`` whose ``on_limit`` is the default
    ``OnLimitConfig`` — confirming the default-completion behaviour.
    """
    _write_yaml(isolated_project / "reyn.yaml", "model: standard\n")
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.on_limit == OnLimitConfig()
    assert cfg.safety == SafetyConfig()


# ─── 3. RunResult.partial_data is None by default + populated on abort ──


def test_runresult_partial_data_default_is_none() -> None:
    """Tier 2: a fresh ``RunResult`` (= ``finished`` happy path) has
    ``partial_data=None``. Callers can use ``None`` to distinguish a
    clean completion from an abort that produced partial output.
    """
    from reyn.kernel.runtime import RunResult

    r = RunResult(data={"final": "ok"}, status="finished")
    assert r.partial_data is None
    assert r.ok is True


def test_runresult_partial_data_populated_on_abort_status() -> None:
    """Tier 2: on an abort (= ``loop_limit_exceeded`` etc.), the
    ``partial_data`` field is the canonical place to surface "what we
    have so far". Constructing a RunResult with both is the contract
    the OS abort paths use.
    """
    from reyn.kernel.runtime import RunResult

    r = RunResult(
        data={"phase_x": "draft"},
        status="loop_limit_exceeded",
        partial_data={"phase_x": "draft"},
        error="Phase 'p1' reached max_phase_visits=25.",
    )
    assert r.ok is False
    assert r.partial_data == {"phase_x": "draft"}
    assert r.status == "loop_limit_exceeded"
