"""Tier 2: regression guards for LiteLLM Gemini model prefix and localization.

Background (#1162 / #1167):
- The `openai/gemini-*` prefix causes litellm catalog lookup to fail → 128K
  fallback instead of the actual 1M context limit.
- The `gemini/gemini-*` prefix resolves correctly.
- Model strings are localised in builtin_models.py; tracked configs use
  class-ref shorthands (e.g. ``gemini-flash-lite``) so future prefix or
  model changes are 1-file updates.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# Tracked config files that must NOT use any hardcoded <provider>/gemini- string.
# Class-ref shorthands (e.g. gemini-flash-lite) are the correct form.
TRACKED_CONFIGS = [
    Path("reyn.yaml"),
    Path("cookbook/configs/with-mcp.yaml"),
    Path("dogfood/fixtures/fizzbuzz_5bugs_interleaved/reyn.yaml"),
    Path("dogfood/fixtures/fizzbuzz_bug_planted/reyn.yaml"),
    Path("dogfood/fixtures/fizzbuzz_tdd/reyn.yaml"),
    Path("dogfood/fixtures/skill_importer_chain/reyn.yaml"),
]

_HARDCODED_GEMINI = re.compile(r"[a-z]+/gemini-")


@pytest.mark.parametrize("rel_path", TRACKED_CONFIGS, ids=str)
def test_no_hardcoded_gemini_model_in_tracked_config(rel_path: Path) -> None:
    """Tier 2: tracked config does not hardcode any <provider>/gemini- model string.

    Use class-ref shorthands (gemini-flash-lite, gemini-pro, …) so model
    strings are localised in builtin_models.py (#1167).
    """
    path = ROOT / rel_path
    assert path.exists(), f"Tracked config not found: {path}"
    content = path.read_text()
    matches = _HARDCODED_GEMINI.findall(content)
    assert not matches, (
        f"{rel_path} contains hardcoded <provider>/gemini- model string "
        f"({len(matches)} occurrence(s)): {matches}. "
        "Use a class-ref shorthand (e.g. gemini-flash-lite) instead — see #1167."
    )


def test_builtin_gemini_entries_use_gemini_prefix() -> None:
    """Tier 2: builtin_models.py gemini entries use gemini/ (not openai/) prefix.

    Ensures the canonical model string in the registry resolves to the 1M-token
    context window, not the 128K fallback caused by the openai/ prefix (#1162).
    """
    from reyn.llm.builtin_models import BUILTIN_MODELS

    for name, entry in BUILTIN_MODELS.items():
        if not name.startswith("gemini"):
            continue
        model = entry.get("model", "")
        assert model.startswith("gemini/"), (
            f"builtin_models['{name}']['model'] = '{model}' does not start "
            "with 'gemini/' prefix. Use gemini/<model> to get 1M context (#1162)."
        )
