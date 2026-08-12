"""Tier 2: the config `reyn init` generates passes `reyn config validate`
clean, including the fields that only reach validate once a user activates
them (#4391).

`reyn init`'s generated `reyn.yaml`, un-edited, failed `reyn config
validate` — the template had never migrated past the pre-#4174-T3
top-level `model:`/`models:` shape. `REYN_LOCAL_CONFIG_TEMPLATE`'s
`api_base:`/`models:` example was additionally wrong INSIDE a comment
block, so no `unknown_config_keys` walk (validate's own, or the startup
warning's) ever saw it — it only bit an operator the moment they
uncommented it to actually use a proxy (#4391's own reproduction: 2
unrecognized keys become 3 the instant `api_base:` is activated).

`git grep -l REYN_YAML_TEMPLATE -- tests/` returned nothing before this
file — the generator's own output had never been run through its own
validator. This closes that gap for both templates, including the
commented-out block, not just what a static read of the template can see.

Asserts the structured result of ``unknown_config_keys`` directly, not
``_validate()``'s printed message — the earlier draft asserted on the
display string, which is a Tier-4 exact-formatting pin (a punctuation
edit to that string would fail all three tests for a reason unrelated to
#4391's actual defect). ``_validate()`` is still called on the CLI path so
the test also exercises the real command, but the pass/fail decision
lives on the same structured result the printed message is itself derived
from (lead-coder review, #4392).
"""
from __future__ import annotations

from pathlib import Path

import pytest


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


def _uncomment_last_paragraph(template: str) -> str:
    """Strip the `# ` (or bare `#`) prefix from every line of the LAST
    blank-line-delimited paragraph in *template* — the commented-out
    example YAML block `REYN_LOCAL_CONFIG_TEMPLATE` ships, isolated from
    the prose paragraphs above it. Mirrors exactly what an operator does
    by hand: uncomment the block they want to use, leave the rest."""
    paragraphs = template.split("\n\n")
    block = paragraphs[-1]
    lines = []
    for line in block.splitlines():
        stripped = line.lstrip()
        assert stripped.startswith("#"), (
            f"expected the last paragraph to be all comment lines, got: {line!r}"
        )
        rest = stripped[1:]
        if rest.startswith(" "):
            rest = rest[1:]
        lines.append(rest)
    return "\n".join(lines)


def _assert_generated_config_is_clean(project: Path) -> None:
    """Run the real `_validate()` (CLI path, still exercised) and then
    assert on the SAME structured result its own report is derived from —
    not the printed message. `build_policy_tier_config` re-reads whatever
    was just written to *project*, the same construction `_validate()` and
    `load_config`'s own startup warning both use."""
    from reyn.config.config_schema import unknown_config_keys
    from reyn.config.loader import build_policy_tier_config
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    assert unknown_config_keys(build_policy_tier_config(project)) == {}


def test_generated_reyn_yaml_passes_validate_unedited(project):
    """Tier 2: `reyn init`'s `reyn.yaml` output, byte-for-byte, is what a
    brand-new project has — it must validate clean with zero edits."""
    from reyn.interfaces.cli.templates import REYN_YAML_TEMPLATE

    (project / "reyn.yaml").write_text(REYN_YAML_TEMPLATE, encoding="utf-8")
    _assert_generated_config_is_clean(project)


def test_generated_reyn_local_yaml_example_passes_validate_as_shipped(project):
    """Tier 2: `REYN_LOCAL_CONFIG_TEMPLATE` as `reyn init` writes it
    (entirely commented out) contributes nothing active — validating it
    unedited must stay clean, same as an absent file."""
    from reyn.interfaces.cli.templates import REYN_LOCAL_CONFIG_TEMPLATE

    (project / "reyn.local.yaml").write_text(REYN_LOCAL_CONFIG_TEMPLATE, encoding="utf-8")
    _assert_generated_config_is_clean(project)


def test_generated_reyn_local_yaml_example_passes_validate_once_activated(project):
    """Tier 2: #4391's real defect — a stale key hidden inside a comment
    block is invisible to every unknown-key walk until an operator
    uncomments it. This activates the block exactly as an operator would
    (uncomment, nothing else) and validates that result, not the raw
    (still-commented) template `_passes_validate_as_shipped` above
    already covers."""
    from reyn.interfaces.cli.templates import REYN_LOCAL_CONFIG_TEMPLATE

    activated = _uncomment_last_paragraph(REYN_LOCAL_CONFIG_TEMPLATE)
    (project / "reyn.local.yaml").write_text(activated, encoding="utf-8")
    _assert_generated_config_is_clean(project)
