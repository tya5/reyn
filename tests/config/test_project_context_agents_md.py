"""Tier 2: project-context resolution adopts AGENTS.md (cross-tool standard).

`load_project_context` resolves the markdown injected into the router system
prompt. The default (project_context_path=None) auto-resolves the cross-tool
standard AGENTS.md (read by Claude Code / Codex / opencode / etc.) and falls
back to the legacy REYN.md — migration-safe. An explicit path pins one file;
"" disables.

Each fixture writes DISTINCT content per file so the assertion pins WHICH file
was read, not merely that some content came back (round-trip on a non-default
value).
"""
from __future__ import annotations

from pathlib import Path

from reyn.config import ReynConfig, load_project_context

_AGENTS = "AGENTS content — cross-tool standard"
_REYN = "REYN content — legacy fallback"
_CLAUDE = "CLAUDE content — explicit pin"


def _write(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


def test_default_reads_agents_md_when_only_agents(tmp_path: Path) -> None:
    """Tier 2: default (None) reads AGENTS.md when it is the only file."""
    _write(tmp_path, "AGENTS.md", _AGENTS)
    cfg = ReynConfig()  # project_context_path defaults to None (auto)
    assert cfg.project_context_path is None
    assert load_project_context(cfg, tmp_path) == _AGENTS


def test_default_falls_back_to_reyn_md_when_only_reyn(tmp_path: Path) -> None:
    """Tier 2: default (None) falls back to REYN.md (migration-safe)."""
    _write(tmp_path, "REYN.md", _REYN)
    cfg = ReynConfig()
    assert load_project_context(cfg, tmp_path) == _REYN


def test_default_prefers_agents_md_when_both_exist(tmp_path: Path) -> None:
    """Tier 2: default (None) prefers AGENTS.md over REYN.md when both exist."""
    _write(tmp_path, "AGENTS.md", _AGENTS)
    _write(tmp_path, "REYN.md", _REYN)
    cfg = ReynConfig()
    assert load_project_context(cfg, tmp_path) == _AGENTS


def test_explicit_path_pins_that_file_over_defaults(tmp_path: Path) -> None:
    """Tier 2: an explicit path wins over the auto-resolved defaults."""
    _write(tmp_path, "AGENTS.md", _AGENTS)
    _write(tmp_path, "REYN.md", _REYN)
    _write(tmp_path, "CLAUDE.md", _CLAUDE)
    cfg = ReynConfig(project_context_path="CLAUDE.md")
    assert load_project_context(cfg, tmp_path) == _CLAUDE


def test_explicit_empty_disables_injection(tmp_path: Path) -> None:
    """Tier 2: explicit "" disables, even when default files exist."""
    _write(tmp_path, "AGENTS.md", _AGENTS)
    cfg = ReynConfig(project_context_path="")
    assert load_project_context(cfg, tmp_path) == ""


def test_default_returns_empty_when_no_file(tmp_path: Path) -> None:
    """Tier 2: default (None) with neither file present yields ""."""
    cfg = ReynConfig()
    assert load_project_context(cfg, tmp_path) == ""


def test_present_but_empty_agents_md_does_not_fall_through(tmp_path: Path) -> None:
    """Tier 2: an existing-but-empty AGENTS.md is authoritative (no REYN.md fallthrough).

    Mirrors opencode's "AGENTS.md beats CLAUDE.md when both exist" — presence,
    not content, decides which file is read.
    """
    _write(tmp_path, "AGENTS.md", "   \n")
    _write(tmp_path, "REYN.md", _REYN)
    cfg = ReynConfig()
    assert load_project_context(cfg, tmp_path) == ""


def test_yaml_omitted_resolves_to_none_default(tmp_path: Path) -> None:
    """Tier 2: a reyn.yaml without project_context_path → None → AGENTS.md read.

    Pins the loader wiring (absent key → None, not the literal "REYN.md") so the
    auto-resolution actually triggers for real configs.
    """
    from reyn.config import load_config

    (tmp_path / "reyn.yaml").write_text("prompt_cache_enabled: true\n", encoding="utf-8")
    _write(tmp_path, "AGENTS.md", _AGENTS)
    cfg = load_config(tmp_path)
    assert cfg.project_context_path is None
    assert load_project_context(cfg, tmp_path) == _AGENTS
