"""Tier 2: #4231 (C) — ``reyn config validate`` reports a KNOWN,
correctly-spelled config key whose value is currently inert because of
ANOTHER key's value (``disabled_config_keys`` /
``_check_universal_wrappers_enabled_scheme_mismatch`` in
``config_schema.py``).

A DIFFERENT defect class than #4174 T0's ``unknown_config_keys`` (a typo
or a key that never existed): here the key IS real and genuinely read
somewhere — architect's own re-measurement (confirmed independently
before writing this test, per the standing "don't build a mechanism on
an assumption" instruction) traced ``tool_use.universal_wrappers_enabled``
(#4552 PR-3: relocated here from ``action_retrieval.
universal_wrappers_enabled`` — architect's ruling, a tool_use/
presentation-scheme property, not a retrieval setting) through
``RouterHostAdapter.get_universal_wrappers_enabled()`` into
``_category_exposure.build_category_exposure`` — imported ONLY by
``universal_category.py``'s ``tool_use.scheme: universal-category`` cell,
never by ``enumerate-all`` (the #1657 owner default) or ``retrieval``.
So the flag is real, but silently a no-op under the default scheme —
architect's own "advertised, and readable, but inert under the current
config as a whole" class, which #3907/#3962's already-established
"nobody ever reads it → delete" discipline does not cover.

#4564 note: this check's underlying inconsistency is UNCHANGED by #4564
(which fixed a separate defect — the flag's undeclared reach into
search_actions visibility). The relocation to ``tool_use.*`` is PR-3's
own move; both keys this check compares now live under the same
top-level ``tool_use:`` key.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.config_schema import disabled_config_keys
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

# ── the pure primitive ───────────────────────────────────────────────────


def test_explicit_true_under_the_default_scheme_is_flagged() -> None:
    """Tier 2: the exact operator shape the issue names — the flag written
    explicitly true, no tool_use.scheme (→ the enumerate-all default)."""
    result = disabled_config_keys({"tool_use": {"universal_wrappers_enabled": True}})
    assert "tool_use.universal_wrappers_enabled" in result
    hint = result["tool_use.universal_wrappers_enabled"]
    assert "enumerate-all" in hint.note
    assert hint.dependency_key == "tool_use.scheme"
    assert "universal-category" in hint.fix


def test_explicit_true_under_universal_category_scheme_is_not_flagged() -> None:
    """Tier 2: accept-side — the ONE scheme that actually reads the flag
    must never trip the warning (a false positive here would teach
    operators to ignore the report entirely, the same #4174 T0 concern)."""
    result = disabled_config_keys({
        "tool_use": {"universal_wrappers_enabled": True, "scheme": "universal-category"},
    })
    assert result == {}


def test_explicit_true_under_retrieval_scheme_is_flagged() -> None:
    """Tier 2: the SAME mismatch under the other non-wrappers scheme
    (retrieval), not just the default — the check is keyed on "is the
    scheme universal-category", not on "is it exactly enumerate-all"."""
    result = disabled_config_keys({
        "tool_use": {"universal_wrappers_enabled": True, "scheme": "retrieval"},
    })
    assert "tool_use.universal_wrappers_enabled" in result
    assert "retrieval" in result["tool_use.universal_wrappers_enabled"].note


def test_key_absent_entirely_is_not_flagged() -> None:
    """Tier 2: regression guard — a config that never touches this key at
    all produces no finding, even though the RESOLVED default is also
    True (firing on the unset default would warn nearly every operator
    who never touched this key — not what "explicit" means in
    architect's ruling)."""
    assert disabled_config_keys({}) == {}
    assert disabled_config_keys({"tool_use": {}}) == {}


def test_explicit_false_is_never_flagged() -> None:
    """Tier 2: an operator who explicitly opted OUT is never told their
    (already-inert, already-intentional) choice is inert."""
    result = disabled_config_keys({
        "tool_use": {"universal_wrappers_enabled": False},
    })
    assert result == {}


# ── the reyn config validate CLI surface ─────────────────────────────────


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr("reyn.config._find_project_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("reyn.config.loader._find_project_root", lambda _cwd: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_validate_reports_the_disabled_key_with_all_four_elements(project, capsys):
    """Tier 2: the CLI wiring — reyn config validate's output carries the
    SAME 4-element discipline #4174 T0 established (architect's explicit
    requirement on #4231's ruling): the RESULT stated plainly, the
    CONFLICTING key named, and a concrete FIX — not just 'this is
    inert'."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + "tool_use:\n  universal_wrappers_enabled: true\n",
    )
    _validate()
    out = capsys.readouterr().out
    assert "tool_use.universal_wrappers_enabled" in out
    assert "no effect" in out
    assert "tool_use.scheme" in out  # the named dependency
    assert "universal-category" in out  # the concrete fix


def test_validate_does_not_flag_universal_category_scheme(project, capsys):
    """Tier 2: CLI accept-side — the correctly-paired config (scheme
    actually set to universal-category) produces the clean report."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML +
        "tool_use:\n  universal_wrappers_enabled: true\n  scheme: universal-category\n",
    )
    _validate()
    out = capsys.readouterr().out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out
