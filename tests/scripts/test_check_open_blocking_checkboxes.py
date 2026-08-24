"""Tier 1: PR bodies must reject open red blocking checkboxes."""
from __future__ import annotations

import json

from scripts.check_open_blocking_checkboxes import evaluate_body, main, run_gate


def test_open_blocking_checkbox_variants_fail() -> None:
    """Tier 1: supported list markers and formatting fail closed."""
    for body in (
        "- [ ] 🔴 unresolved",
        "  - [ ] 🔴 nested",
        "* [ ]🔴 compact",
        "+ [ ] **🔴** decorated",
    ):
        code, _ = evaluate_body(body)
        assert code != 0


def test_checked_blocking_checkbox_passes() -> None:
    """Tier 1: a checked red blocking checkbox passes the gate."""
    code, _ = evaluate_body("- [x] 🔴 resolved")
    assert code == 0


def test_missing_body_fails_closed() -> None:
    """Tier 1: a missing PR body fails closed rather than passing vacuously."""
    code, _ = evaluate_body(None)
    assert code != 0


def test_body_supplier_failure_fails_closed() -> None:
    """Tier 1: a failed body supplier returns nonzero without patching."""
    def fail():
        raise OSError("network down")

    assert run_gate(fail) != 0


def test_fixture_cli_supports_both_states(tmp_path) -> None:
    """Tier 1: the CLI reports both open and checked fixture bodies."""
    fixture = tmp_path / "pr.json"
    fixture.write_text(json.dumps({"body": "- [x] 🔴 done"}), encoding="utf-8")
    assert main(["--fixture", str(fixture)]) == 0
    fixture.write_text(json.dumps({"body": "- [ ] 🔴 todo"}), encoding="utf-8")
    assert main(["--fixture", str(fixture)]) != 0
