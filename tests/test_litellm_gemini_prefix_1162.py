"""Tier 2: regression guard — tracked config files must not use openai/<gemini> prefix.

Background (#1162): the `openai/gemini-*` LiteLLM model prefix causes litellm
to fall back to a 128K context window (catalog lookup fails, hardcoded default
kicks in). The correct prefix for Gemini models via an OpenAI-compatible proxy
is `gemini/gemini-*`, which resolves to the actual 1M-token context limit.

This test prevents silent regressions where someone reverts the prefix change in
a tracked yaml file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# Tracked config files that must NOT use openai/<gemini> prefix.
# Each entry is a path relative to repo root.
TRACKED_CONFIGS = [
    Path("reyn.yaml"),
    Path("cookbook/configs/with-mcp.yaml"),
    Path("dogfood/fixtures/fizzbuzz_5bugs_interleaved/reyn.yaml"),
    Path("dogfood/fixtures/fizzbuzz_bug_planted/reyn.yaml"),
    Path("dogfood/fixtures/fizzbuzz_tdd/reyn.yaml"),
    Path("dogfood/fixtures/skill_importer_chain/reyn.yaml"),
]

_BAD_PREFIX = re.compile(r"openai/gemini-")


@pytest.mark.parametrize("rel_path", TRACKED_CONFIGS, ids=str)
def test_no_openai_gemini_prefix_in_tracked_config(rel_path: Path) -> None:
    """Tier 2: tracked config does not use openai/<gemini> model prefix.

    The openai/ prefix for Gemini models causes litellm to fall back to a 128K
    context window. Use gemini/ prefix instead (#1162).
    """
    path = ROOT / rel_path
    assert path.exists(), f"Tracked config not found: {path}"
    content = path.read_text()
    matches = _BAD_PREFIX.findall(content)
    assert not matches, (
        f"{rel_path} contains openai/<gemini> model prefix "
        f"({len(matches)} occurrence(s)). "
        "Use gemini/<model> prefix instead — see #1162 for background."
    )
