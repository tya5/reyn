"""Tier 1: self-test for Rule 9 (#5103, ``scripts/test_tier_audit.py``'s
``_has_llm_stub_marker`` + the Rule 9 block) — @llm_stub must not declare
Tier 3.

``@pytest.mark.llm_stub`` always returns the SAME fixed minimal completion
regardless of what was asked (architect design "C2", #5103) — so a test
using it can never have the model's own output as its subject, which is
exactly what Tier 3 means (#5294's discriminator). This rule enforces that
pairing statically, driven through the real ``main()`` CLI entry point (not
the detector called in isolation) — the same "wiring, not just correctness"
concern #4904 (Rule 7) named for its own rule.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "test_tier_audit.py"


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` without pytest collecting it as
    a test module — same loader idiom as
    ``test_tier_audit_fake_attr_self_test_4904.py``."""
    spec = importlib.util.spec_from_file_location("_audit_llm_stub_tier_5103", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_llm_stub_plus_tier_3_is_flagged(tmp_path: Path) -> None:
    """Tier 1: @llm_stub + a Tier 3 docstring is a Rule 9 violation, driven
    through the real main() CLI entry point."""
    audit = _load_audit_module()
    bad = tmp_path / "test_bad_combo.py"
    bad.write_text(
        "import pytest\n\n"
        "@pytest.mark.llm_stub\n"
        "async def test_bad_combo():\n"
        '    """Tier 3a: falsely claims the model output is the subject."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    assert audit.main(["--check", "llm-stub-tier", str(bad)]) != 0


def test_llm_stub_plus_tier_2_is_not_flagged(tmp_path: Path) -> None:
    """Tier 1: @llm_stub + Tier 2 (the actual pairing this seam is for) is
    clean — accept-side, without this a Rule 9 that fires unconditionally on
    @llm_stub (regardless of the declared Tier) would pass the reject-side
    test above and go unnoticed."""
    audit = _load_audit_module()
    good = tmp_path / "test_good_combo.py"
    good.write_text(
        "import pytest\n\n"
        "@pytest.mark.llm_stub\n"
        "async def test_good_combo():\n"
        '    """Tier 2: loop/valve behavior, not the model output."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    assert audit.main(["--check", "llm-stub-tier", str(good)]) == 0


def test_tier_3_without_llm_stub_is_not_flagged_by_this_rule(tmp_path: Path) -> None:
    """Tier 1: Rule 9 only fires on the @llm_stub + Tier 3 PAIRING — an
    ordinary @replay Tier 3 test (the case this seam does not touch) must
    not be caught by this rule."""
    audit = _load_audit_module()
    ordinary = tmp_path / "test_ordinary_tier3.py"
    ordinary.write_text(
        "import pytest\n\n"
        '@pytest.mark.replay("some_fixture.jsonl")\n'
        "async def test_ordinary_tier3():\n"
        '    """Tier 3a: the model output really is the subject here."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    assert audit.main(["--check", "llm-stub-tier", str(ordinary)]) == 0
