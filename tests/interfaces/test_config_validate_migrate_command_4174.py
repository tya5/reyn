"""Tier 2: ``reyn config validate`` / ``reyn config migrate`` CLI commands
(#4174 T0/T0b).

``validate`` reports unknown/renamed config keys via the SAME
``build_policy_tier_config`` construction ``load_config``'s own startup
warning uses (architect's explicit requirement). ``migrate`` auto-rewrites
only unambiguous plain renames (``_RENAMED_CONFIG_KEYS`` entries whose hint
is a bare dotted key, no value transform) and reports the rest as needing
manual review — see ``_migrate``'s own docstring for why a prose hint (e.g.
sandbox.policy's value-inverting renames) is never auto-rewritten.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.setattr(
        "reyn.config.loader._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── validate ───────────────────────────────────────────────────────────


def test_validate_reports_no_findings_on_a_well_formed_config(project, capsys):
    """Tier 2: #4174 T0 accept-side — a real, valid reyn.yaml produces
    'no unknown keys' output, not a false-positive warning. #4231 (C)
    widened the message when it added the disabled-by-dependency
    category (a well-formed config trips neither)."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", "model: standard\nsandbox:\n  mode: strict\n")
    _validate()
    out = capsys.readouterr().out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


def test_validate_reports_an_unrecognized_top_level_key(project, capsys):
    """Tier 2: #4174 T0 — an unrecognized key is named in the report with
    the NOT-APPLIED framing (mirrors load_config's own startup warning
    text — same underlying mechanism, see build_policy_tier_config)."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", "totally_made_up_top_level_key: 1\n")
    _validate()
    out = capsys.readouterr().out
    assert "totally_made_up_top_level_key" in out


def test_validate_reports_llm_model_as_a_real_pre_existing_dead_key(project, capsys):
    """Tier 2: #4174 T0 — architect's confirmed live pre-existing defect
    (LLMConfig has no `model` field) surfaces through the CLI report too,
    not just the startup warning — same underlying walk."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", "llm:\n  model: standard\n")
    _validate()
    out = capsys.readouterr().out
    assert "llm.model" in out


def test_validate_subcommand_is_registered():
    """Tier 2: ``reyn config validate`` is registered as a subcommand."""
    import argparse

    from reyn.interfaces.cli.commands.config import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    args = parser.parse_args(["config", "validate"])
    assert args.config_cmd == "validate"


# ── migrate ────────────────────────────────────────────────────────────


def test_migrate_reports_nothing_to_migrate_when_registry_is_empty(
    project, capsys, monkeypatch,
):
    """Tier 2: #4174 T0b — with ``_RENAMED_CONFIG_KEYS`` empty, ``reyn
    config migrate`` says so explicitly rather than silently doing nothing
    with no output (lead-coder's explicit requirement: must not be
    silently vacuous).

    Injects an empty registry explicitly (the same ``monkeypatch.setattr``
    seam the sibling test below already uses for a non-empty one) — #4174
    T5 populated ``_RENAMED_CONFIG_KEYS`` for real, which falsified this
    test's original premise that the module-global registry was ITSELF
    empty at the time the test ran. What this test actually verifies is
    "what `migrate` says when the registry is empty", not "is the
    registry currently empty" — those are different claims, and only the
    first one is this test's to make."""
    from reyn.interfaces.cli.commands.config import _migrate

    monkeypatch.setattr("reyn.config.config_schema._RENAMED_CONFIG_KEYS", {})
    _write_yaml(project / "reyn.yaml", "model: standard\n")
    _migrate()
    out = capsys.readouterr().out
    assert "No config key renames are registered yet" in out


def test_migrate_reports_nothing_to_migrate_when_config_has_no_renamed_key(
    project, capsys, monkeypatch,
) -> None:
    """Tier 2: #4174 T0b — the OTHER "nothing to migrate" reason: renames
    ARE registered, but this project's config doesn't use any of the old
    keys. Distinct code path from the empty-registry case above (both real,
    per lead-coder's requirement)."""
    from reyn.config.config_schema import RenamedKeyHint
    from reyn.interfaces.cli.commands.config import _migrate

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_top_level_key": RenamedKeyHint(
            note="moved to new_top_level_key", destination="new_top_level_key",
        )},
    )
    _write_yaml(project / "reyn.yaml", "model: standard\n")
    _migrate()
    out = capsys.readouterr().out
    assert "nothing to migrate" in out.lower()


def test_migrate_rewrites_an_unambiguous_plain_rename(project, capsys, monkeypatch) -> None:
    """Tier 2: #4174 T0b — an entry whose hint has a non-None
    ``destination`` (a plain rename, no value transform) is auto-rewritten
    in place, old key removed (lead-coder's block on #4190: the decision
    is a typed field, not a syntactic proxy)."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_flat_key": RenamedKeyHint(
            note="moved to new.nested.key", destination="new.nested.key",
        )},
    )
    _write_yaml(project / "reyn.yaml", "model: standard\nold_flat_key: hello\n")

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    import yaml
    cfg = yaml.safe_load((project / "reyn.yaml").read_text())
    assert "old_flat_key" not in cfg
    assert cfg["new"]["nested"]["key"] == "hello"
    out = capsys.readouterr().out
    assert "old_flat_key -> new.nested.key" in out


def test_migrate_dry_run_does_not_write(project, capsys, monkeypatch) -> None:
    """Tier 2: #4174 T0b — ``--dry-run`` previews the rewrite without
    touching the file (mirrors migrate-mcp's own dry-run contract)."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_flat_key": RenamedKeyHint(
            note="moved to new_flat_key", destination="new_flat_key",
        )},
    )
    _write_yaml(project / "reyn.yaml", "old_flat_key: hello\n")

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate(dry_run=True)

    import yaml
    cfg = yaml.safe_load((project / "reyn.yaml").read_text())
    assert "old_flat_key" in cfg
    out = capsys.readouterr().out
    assert "Dry run only" in out


def test_migrate_flags_a_value_transforming_rename_for_manual_review(
    project, capsys, monkeypatch,
) -> None:
    """Tier 2: #4174 T0b — an entry whose ``destination`` is None (a value
    transform, not a plain rename) is NOT auto-rewritten; it's reported as
    needing manual review instead (see _migrate's docstring for why
    guessing at the transform would be unsafe)."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_inverting_key": RenamedKeyHint(
            note="moved to new_key, value inverts", destination=None,
        )},
    )
    _write_yaml(project / "reyn.yaml", "old_inverting_key: true\n")

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    import yaml
    cfg = yaml.safe_load((project / "reyn.yaml").read_text())
    # Untouched — not auto-rewritten.
    assert cfg["old_inverting_key"] is True
    out = capsys.readouterr().out
    assert "manual review" in out.lower()
    assert "old_inverting_key" in out


def test_migrate_does_not_rewrite_a_space_free_note_with_no_destination(
    project, capsys, monkeypatch,
) -> None:
    """Tier 2: #4174 T0b — lead-coder's exact concern on #4190: the OLD
    design decided auto-rewrite eligibility from whether the hint STRING
    happened to contain a space, so a value-transforming rename whose note
    was accidentally written without one would have been silently
    auto-applied. With destination as its own typed field, a space-free
    note with destination=None is correctly left untouched regardless of
    its text shape."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_key": RenamedKeyHint(note="inverted-no-spaces-here", destination=None)},
    )
    _write_yaml(project / "reyn.yaml", "old_key: true\n")

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    import yaml
    cfg = yaml.safe_load((project / "reyn.yaml").read_text())
    assert cfg["old_key"] is True  # untouched
    out = capsys.readouterr().out
    assert "manual review" in out.lower()


def test_migrate_subcommand_is_registered():
    """Tier 2: ``reyn config migrate`` is registered as a subcommand."""
    import argparse

    from reyn.interfaces.cli.commands.config import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    args = parser.parse_args(["config", "migrate", "--dry-run"])
    assert args.config_cmd == "migrate"
    assert args.dry_run is True
