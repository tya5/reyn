"""Tier 2: OS invariant — skill_resume policy parsing from reyn.yaml.

The resume runtime queries `SkillResumeConfig.policy_for(skill_name)` to
decide what to do with ambiguous steps. A misparse here would either
silently fall back to "prompt" (annoying but safe) or — worse — accept
an invalid policy string and produce undefined behavior in the resume
flow. This file pins:

  - Default config values
  - Unknown policy strings are rejected with a fall-back to the default
  - Per-skill overrides are honored when valid, silently dropped when
    invalid
  - YAML-loaded configs round-trip through the dataclass correctly

Observation: the `SkillResumeConfig` instance returned by
`_build_skill_resume_config` (and ultimately `load_config`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    SKILL_RESUME_POLICIES,
    SkillResumeConfig,
    _build_skill_resume_config,
    load_config,
)


def test_skill_resume_default_values():
    """Tier 2: default config has policy=prompt and empty per_skill."""
    cfg = SkillResumeConfig()
    assert cfg.default == "prompt"
    assert cfg.per_skill == {}
    assert cfg.policy_for("anything") == "prompt"


def test_skill_resume_policy_for_falls_back_to_default():
    """Tier 2: policy_for returns per_skill override if present, else default."""
    cfg = SkillResumeConfig(
        default="prompt",
        per_skill={"routing": "retry", "blog_publisher": "discard_skill"},
    )
    assert cfg.policy_for("routing") == "retry"
    assert cfg.policy_for("blog_publisher") == "discard_skill"
    assert cfg.policy_for("not_listed") == "prompt"


def test_build_returns_default_when_raw_is_not_dict():
    """Tier 2: non-dict input (string, None, list) yields the defaults."""
    assert _build_skill_resume_config(None).default == "prompt"
    assert _build_skill_resume_config("retry").default == "prompt"
    assert _build_skill_resume_config([]).default == "prompt"


def test_build_accepts_all_known_policy_values():
    """Tier 2: every policy in SKILL_RESUME_POLICIES round-trips through build."""
    for policy in SKILL_RESUME_POLICIES:
        cfg = _build_skill_resume_config({"default": policy})
        assert cfg.default == policy


def test_build_rejects_unknown_default_with_fallback(caplog):
    """Tier 2: an unknown default policy triggers a warning + fallback to 'prompt' (never crashes startup)."""
    cfg = _build_skill_resume_config({"default": "auto_yolo"})
    assert cfg.default == "prompt"  # fall-back


def test_build_per_skill_overrides_round_trip():
    """Tier 2: valid per_skill mappings appear in the parsed config verbatim."""
    cfg = _build_skill_resume_config({
        "default": "retry",
        "per_skill": {
            "routing": "retry",
            "blog_publisher": "prompt",
            "text_summariser": "skip",
        },
    })
    assert cfg.default == "retry"
    assert cfg.per_skill == {
        "routing": "retry",
        "blog_publisher": "prompt",
        "text_summariser": "skip",
    }


def test_build_per_skill_drops_invalid_values():
    """Tier 2: an invalid per_skill policy value is logged and silently dropped (other entries survive)."""
    cfg = _build_skill_resume_config({
        "per_skill": {
            "good_skill": "retry",
            "bad_skill": "frobnicate",  # invalid
        },
    })
    assert cfg.per_skill == {"good_skill": "retry"}


def test_build_per_skill_non_dict_yields_empty():
    """Tier 2: per_skill that isn't a dict (e.g. a list) is silently skipped."""
    cfg = _build_skill_resume_config({"per_skill": ["bad", "format"]})
    assert cfg.per_skill == {}


def test_load_config_picks_up_skill_resume_yaml(tmp_path, monkeypatch):
    """Tier 2: full load_config integration — skill_resume block in reyn.yaml is parsed end-to-end."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text(
        "skill_resume:\n"
        "  default: retry\n"
        "  per_skill:\n"
        "    blog_publisher: prompt\n"
        "    log_uploader: skip\n",
        encoding="utf-8",
    )
    # Stub a project root marker (reyn.yaml itself is sufficient — _find_project_root
    # walks up looking for it).
    cfg = load_config(cwd=tmp_path)
    assert cfg.skill_resume.default == "retry"
    assert cfg.skill_resume.policy_for("blog_publisher") == "prompt"
    assert cfg.skill_resume.policy_for("log_uploader") == "skip"
    assert cfg.skill_resume.policy_for("never_listed") == "retry"


def test_load_config_default_when_no_skill_resume_block(tmp_path, monkeypatch):
    """Tier 2: a reyn.yaml without skill_resume gets the default (prompt) policy."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.skill_resume.default == "prompt"
    assert cfg.skill_resume.per_skill == {}
