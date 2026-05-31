"""Tier 2: pure regex parser for self_improvement subset of reyn.yaml (R-PURE-MODE Wave 3b).

Tests parse_on_propose_config_minimal — the safe-mode replacement for
read_on_propose_config (unsafe).  All test inputs are in-process strings;
no filesystem access, no mocks, no private-state assertions.

Testing policy: Tier 2 (OS invariant — deterministic, pure function).
"""
from __future__ import annotations

import textwrap

import pytest

from reyn.stdlib.skills.skill_improver.version_snapshot_pure import (
    parse_on_propose_config_minimal,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _artifact(yaml_text: str) -> dict:
    """Build the artifact dict as the preprocessor would pass it."""
    return {"data": {"_reyn_yaml_text": yaml_text}}


def _artifact_missing() -> dict:
    """Artifact with no _reyn_yaml_text key (= file_read step used on_error: skip)."""
    return {"data": {}}


# ── test cases ─────────────────────────────────────────────────────────────────


def test_parse_on_propose_defaults_when_no_self_improvement_block():
    """Tier 2: yaml with no self_improvement: key yields defaults {ask_user, 10}."""
    yaml = textwrap.dedent("""\
        model: standard
        models:
          light: openai/gemini-2.5-flash-lite
          standard: openai/gemini-2.5-flash-lite
        permissions:
          python.pure: allow
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result == {"on_propose": "ask_user", "max_versions": 10}


def test_parse_on_propose_ask_user():
    """Tier 2: on_propose: ask_user is parsed correctly from self_improvement block."""
    yaml = textwrap.dedent("""\
        model: standard
        self_improvement:
          on_propose: ask_user
          max_versions: 10
        limits:
          max_act_turns: 20
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result["on_propose"] == "ask_user"
    assert result["max_versions"] == 10


def test_parse_on_propose_auto():
    """Tier 2: on_propose: auto is parsed correctly."""
    yaml = textwrap.dedent("""\
        self_improvement:
          on_propose: auto
          max_versions: 10
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result["on_propose"] == "auto"


def test_parse_on_propose_disabled():
    """Tier 2: on_propose: disabled is parsed correctly."""
    yaml = textwrap.dedent("""\
        self_improvement:
          on_propose: disabled
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result["on_propose"] == "disabled"
    # max_versions not set → default
    assert result["max_versions"] == 10


def test_parse_on_propose_max_versions_5():
    """Tier 2: max_versions: 5 is parsed as integer 5."""
    yaml = textwrap.dedent("""\
        self_improvement:
          on_propose: auto
          max_versions: 5
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result["max_versions"] == 5


def test_parse_on_propose_ignores_unrelated_top_level_keys():
    """Tier 2: other top-level keys do not interfere with self_improvement parsing."""
    yaml = textwrap.dedent("""\
        # Project-wide config
        model: standard
        models:
          light: openai/gemini-2.5-flash-lite
          standard: openai/gemini-2.5-flash-lite
          strong: openai/gemini-2.5-flash-lite
        chat:
          compaction:
            trigger_total_tokens: 30000
        self_improvement:
          on_propose: auto
          max_versions: 7
        limits:
          max_act_turns: 20
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result["on_propose"] == "auto"
    assert result["max_versions"] == 7


def test_parse_on_propose_ignores_nested_top_level_key_collision():
    """Tier 2: on_propose under another top-level section is not read.

    A sibling block that also has an `on_propose:` indented under it must
    not override the value read from self_improvement.
    """
    yaml = textwrap.dedent("""\
        other_section:
          on_propose: other_value
          max_versions: 99
        self_improvement:
          on_propose: disabled
          max_versions: 3
        yet_another:
          on_propose: collision_value
    """)
    result = parse_on_propose_config_minimal(_artifact(yaml))
    assert result["on_propose"] == "disabled"
    assert result["max_versions"] == 3


def test_parse_on_propose_defaults_when_yaml_text_absent():
    """Tier 2: missing _reyn_yaml_text key (file_read skipped) yields defaults."""
    result = parse_on_propose_config_minimal(_artifact_missing())
    assert result == {"on_propose": "ask_user", "max_versions": 10}
