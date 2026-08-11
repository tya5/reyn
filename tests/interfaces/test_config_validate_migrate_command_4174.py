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

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


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

    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML + "sandbox:\n  mode: strict\n")
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


def test_validate_reports_llm_model_as_known_not_flagged(project, capsys):
    """Tier 2: #4174 T3 — accept-side, superseding this test's former pre-T3
    shape (architect's #4174 T0 finding: `LLMConfig` had no `model` field, so
    `llm: {model: ...}` was silently discarded and flagged unknown by the CLI
    report too, same walk as the startup warning). T3 gave `LLMConfig` a real
    `model` field, so the SAME input must now report clean."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _validate()
    out = capsys.readouterr().out
    assert "llm.model" not in out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


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
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
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
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _migrate()
    out = capsys.readouterr().out
    assert "nothing to migrate" in out.lower()


def test_migrate_rewrites_an_unambiguous_plain_rename(project, capsys, monkeypatch) -> None:
    """Tier 2: #4174 T0b — an entry whose hint has a non-None
    ``destination`` (a plain rename, no value transform) is auto-rewritten
    in place, old key removed (lead-coder's block on #4190: the decision
    is a typed field, not a syntactic proxy).

    Destination is one level of nesting (``new_parent.key``), not two
    (``new.nested.key`` as this test originally used) — #4295's
    comment-preserving text rewrite deliberately supports only 0 or 1
    levels of destination nesting (every currently-registered real rename
    is one of those two shapes; see ``migrate_text``'s module docstring
    for why deeper nesting is refused rather than guessed at). This test's
    own intent (a plain rename WITH nesting is auto-rewritten) is
    unaffected by narrowing the example to a supported shape."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_flat_key": RenamedKeyHint(
            note="moved to new_parent.key", destination="new_parent.key",
        )},
    )
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML + "old_flat_key: hello\n")

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    import yaml
    cfg = yaml.safe_load((project / "reyn.yaml").read_text())
    assert "old_flat_key" not in cfg
    assert cfg["new_parent"]["key"] == "hello"
    out = capsys.readouterr().out
    assert "old_flat_key -> new_parent.key" in out


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


# ── #4295: comment-preserving rewrite ────────────────────────────────────
#
# The owner hit this directly: `yaml.safe_load` + `yaml.dump` round-tripped
# the WHOLE file through PyYAML's comment-blind loader on every migrate run,
# silently dropping every operator comment (API-key warnings, rename
# history, why a key was removed) — not just on the renamed keys, on the
# whole file. `_migrate` now rewrites only the renamed keys' own lines via
# `reyn.config.migrate_text.rewrite_text`, verified against an independent
# structural re-check (`reyn.config.migrate_check.verify_rewrite`) before
# ever being written.


def test_migrate_preserves_a_leading_comment_on_an_in_place_rename(
    project, monkeypatch,
) -> None:
    """Tier 2: #4295 — a same-depth rename keeps the operator's own
    explanatory comment directly above the key, unchanged."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"events": RenamedKeyHint(
            note="moved to audit_events", destination="audit_events",
        )},
    )
    _write_yaml(project / "reyn.yaml", (
        "# API keys must be set as environment variables — never in config files.\n"
        "events:\n"
        "  keep_days: 30\n"
    ))

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    text = (project / "reyn.yaml").read_text()
    assert "# API keys must be set as environment variables" in text
    assert "audit_events:" in text
    assert "events:" not in text.replace("audit_events:", "")


def test_migrate_preserves_an_inline_comment_on_an_in_place_rename(
    project, monkeypatch,
) -> None:
    """Tier 2: #4295 — the owner's report explicitly named inline
    (same-line, ``key: value  # comment``) comments as also lost; this
    pins them surviving specifically, not just leading ones."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"api_base": RenamedKeyHint(
            note="moved to llm.api_base", destination="llm.api_base",
        )},
    )
    _write_yaml(project / "reyn.yaml", (
        "llm:\n  router:\n    use: false\n"
        "api_base: http://localhost:8000  # ← 追加\n"
    ))

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    text = (project / "reyn.yaml").read_text()
    assert "# ← 追加" in text
    assert "http://localhost:8000" in text


def test_migrate_creates_a_new_parent_block_preserving_comments_and_a_nested_value(
    project, monkeypatch,
) -> None:
    """Tier 2: #4295 — a nesting move (`model`/`models` -> `llm.*`) creates
    a NEW `llm:` block, carrying the moved keys' own leading comments and a
    multi-line nested mapping value (`models:`'s own sub-keys), correctly
    re-indented as children of `llm:`."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {
            "model": RenamedKeyHint(note="moved to llm.model", destination="llm.model"),
            "models": RenamedKeyHint(note="moved to llm.models", destination="llm.models"),
        },
    )
    _write_yaml(project / "reyn.yaml", (
        "# FP-0014 renamed python.pure -> python.safe\n"
        "model: standard\n"
        "models:\n"
        "  standard: openai/gpt-4\n"
        "  light: openai/gpt-4-mini\n"
        "allowed_openai_params: [\"tool_choice\"]\n"
    ))

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    text = (project / "reyn.yaml").read_text()
    assert "llm:" in text
    assert "# FP-0014 renamed python.pure -> python.safe" in text
    # The nested value's own sub-keys survived, re-indented under `llm:`.
    assert "    standard: openai/gpt-4" in text
    assert "    light: openai/gpt-4-mini" in text
    # A key this migrate run never touched keeps its exact original
    # (flow-style) formatting — #4295's second finding: the old
    # yaml.dump round-trip reformatted EVERY key in the file, not just
    # the renamed ones (`["tool_choice"]` became block-style).
    assert 'allowed_openai_params: ["tool_choice"]' in text

    import yaml
    cfg = yaml.safe_load(text)
    assert cfg["llm"]["model"] == "standard"
    assert cfg["llm"]["models"] == {"standard": "openai/gpt-4", "light": "openai/gpt-4-mini"}
    assert cfg["allowed_openai_params"] == ["tool_choice"]


def test_migrate_appends_to_an_existing_parent_block(project, monkeypatch) -> None:
    """Tier 2: #4295 — when the destination's parent block already exists
    (an operator who already has an `llm:` section), the moved key is
    APPENDED to it rather than creating a conflicting second `llm:` key
    (which would be invalid YAML)."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"api_base": RenamedKeyHint(
            note="moved to llm.api_base", destination="llm.api_base",
        )},
    )
    _write_yaml(project / "reyn.yaml", (
        "llm:\n  router:\n    use: false\n"
        "api_base: http://localhost:8000\n"
    ))

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    import yaml
    text = (project / "reyn.yaml").read_text()
    assert text.count("llm:") == 1  # never a second top-level llm: key
    cfg = yaml.safe_load(text)
    assert cfg["llm"]["router"]["use"] is False
    assert cfg["llm"]["api_base"] == "http://localhost:8000"


def test_migrate_refuses_an_unsupported_shape_and_leaves_the_file_untouched(
    project, capsys, monkeypatch,
) -> None:
    """Tier 2: #4295 — a destination with more than one level of nesting is
    out of this rewriter's deliberately narrow scope (see
    ``migrate_text``'s module docstring); it's reported as needing manual
    review, and the file is left completely untouched rather than
    partially rewritten."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {"old_key": RenamedKeyHint(
            note="moved to a.b.c", destination="a.b.c",
        )},
    )
    original = "# a comment\nold_key: hello\n"
    _write_yaml(project / "reyn.yaml", original)

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    text = (project / "reyn.yaml").read_text()
    assert text == original, "file must be byte-identical when the rewrite is refused"
    out = capsys.readouterr().out
    assert "manual review" in out.lower()
    assert "old_key" in out


def test_migrate_value_identity_survives_a_multi_key_rewrite(project, monkeypatch) -> None:
    """Tier 2: #4295, the load-bearing test — every value in a config with
    several renamed keys (mixing same-depth and nesting-move destinations)
    is bit-for-bit preserved after migrate, checked by flattening both the
    pre- and post-migrate parsed structures and comparing (the same manual
    technique used to verify the owner's real recovered file, now
    automated instead of one-off)."""
    from reyn.config.config_schema import RenamedKeyHint

    monkeypatch.setattr(
        "reyn.config.config_schema._RENAMED_CONFIG_KEYS",
        {
            "events": RenamedKeyHint(note="-> audit_events", destination="audit_events"),
            "model": RenamedKeyHint(note="-> llm.model", destination="llm.model"),
            "api_base": RenamedKeyHint(note="-> llm.api_base", destination="llm.api_base"),
        },
    )
    original_text = (
        "# a leading comment\n"
        "model: standard\n"
        "api_base: http://localhost:8000  # inline\n"
        "events:\n  keep_days: 30\n"
        "unrelated_key: [1, 2, 3]\n"
    )
    _write_yaml(project / "reyn.yaml", original_text)

    import yaml
    before = yaml.safe_load(original_text)

    from reyn.interfaces.cli.commands.config import _migrate
    _migrate()

    after = yaml.safe_load((project / "reyn.yaml").read_text())

    def _flatten(d, prefix="") -> dict:
        out = {}
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
        return out

    before_flat = _flatten(before)
    after_flat = _flatten(after)
    # Apply the SAME renames to the "before" side, then the two flattened
    # maps must be identical — no value silently changed or dropped.
    renamed_before = {
        (
            k.replace("events", "audit_events", 1) if k.startswith("events")
            else k.replace("model", "llm.model", 1) if k == "model"
            else k.replace("api_base", "llm.api_base", 1) if k == "api_base"
            else k
        ): v
        for k, v in before_flat.items()
    }
    assert renamed_before == after_flat
