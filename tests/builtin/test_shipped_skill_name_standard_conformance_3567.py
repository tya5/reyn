"""Tier 1: Contract -- every skill reyn SHIPS spells its ``name`` the way the
Agent Skills specification requires (#3567).

**The contract this pins.** ``docs/concepts/tools-integrations/skills.md``
calls ``SKILL.md`` "an industry-standard file" and points at the
`Agent Skills specification <https://agentskills.io/specification>`_. That
specification constrains ``name`` to:

- max 64 characters;
- lowercase letters, numbers, and hyphens ONLY;
- no leading or trailing hyphen;
- no consecutive hyphens;
- it must match the parent directory name.

Before #3567 all four shipped skills used underscores (``draft_judge_revise``
etc.) -- conformant on every other clause, non-conformant on the separator --
so the doc's "industry-standard" claim was false for exactly one field. This
module is the gate that keeps it true.

**Scope: what reyn SHIPS, not what reyn ACCEPTS.** reyn deliberately does not
enforce this rule on an operator-registered or third-party skill (the ``:name``
token grammar and the ``skill_install_*`` path-safety check both still accept
``_``); rejecting a non-conformant third-party ``SKILL.md`` outright is a
separate decision. So this gate enumerates the shipping surfaces only.

**Enumeration -- from the registry + a real directory walk, never a hardcoded
name list.** A gate that hard-codes the four post-#3567 strings would certify
the four names already fixed and say nothing about the fifth skill someone adds
next month, which is the only failure this gate exists to catch. The two
surfaces (mirroring ``tests/builtin/test_skill_md_default_inline_cap_gate.py``, which
unions the same pair for the same reason):

1. ``BUILTIN_SKILLS`` (``src/reyn/builtin/registry.py``) -- the always-on
   builtin tier. Both its KEY (what an operator config entry collides with,
   and what ``:name`` invocation resolves against) and the ``SKILL.md``
   frontmatter ``name`` are checked, plus their agreement.
2. Every ``SKILL.md`` under ``src/reyn/builtin/plugins/*/skills/*/`` -- the
   plugin-shipped skills, which register through the install-time
   ``plugin_install`` path rather than ``BUILTIN_SKILLS`` and are therefore
   invisible to a registry-only walk.

No fakes: the real registry map, the real files on disk, and the real
``reyn.core.frontmatter.split_frontmatter`` the runtime itself uses.
"""
from __future__ import annotations

import re
from pathlib import Path

import reyn.builtin.registry as registry_module
from reyn.builtin.registry import BUILTIN_SKILLS
from reyn.core.frontmatter import split_frontmatter

#: The specification's ``name`` grammar, expressed so that all four character
#: clauses are ONE pattern: lowercase-alphanumeric runs joined by single
#: hyphens simultaneously forbids uppercase, forbids any other separator,
#: forbids a leading/trailing hyphen, and forbids consecutive hyphens.
_STANDARD_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: The specification's ``name`` length maximum.
_STANDARD_NAME_MAX_CHARS = 64

_BUILTIN_DIR = Path(registry_module.__file__).parent
_PLUGINS_DIR = _BUILTIN_DIR / "plugins"


def _nonconformance(name: str) -> "str | None":
    """Why ``name`` violates the specification, or ``None`` if it conforms."""
    if len(name) > _STANDARD_NAME_MAX_CHARS:
        return f"{len(name)} chars exceeds the {_STANDARD_NAME_MAX_CHARS}-char maximum"
    if not _STANDARD_NAME_RE.match(name):
        return (
            "must be lowercase letters/numbers joined by single hyphens "
            "(no underscore, no uppercase, no leading/trailing or "
            "consecutive hyphen)"
        )
    return None


def _plugin_skill_md_paths() -> "list[Path]":
    """Every plugin-shipped ``SKILL.md``, by directory walk rather than a
    hardcoded plugin/skill name list, so a newly added plugin skill is
    covered automatically."""
    if not _PLUGINS_DIR.is_dir():
        return []
    return sorted(p.resolve() for p in _PLUGINS_DIR.glob("*/skills/*/SKILL.md"))


def _frontmatter_name(path: Path) -> "str | None":
    fm, _body = split_frontmatter(path.read_text(encoding="utf-8"))
    value = fm.get("name")
    return value if isinstance(value, str) else None


def test_builtin_registry_keys_conform_to_the_standard_name_grammar() -> None:
    """Tier 1: every ``BUILTIN_SKILLS`` key -- the spelling an operator types
    after ``:`` and the spelling a config entry collides on -- satisfies the
    Agent Skills specification's ``name`` rule."""
    assert len(BUILTIN_SKILLS) >= 1, (
        "vacuity guard: BUILTIN_SKILLS is empty -- this gate would pass "
        "with nothing to check"
    )
    violations = {
        name: why
        for name in BUILTIN_SKILLS
        if (why := _nonconformance(name)) is not None
    }
    assert not violations, (
        "BUILTIN_SKILLS key violates the Agent Skills specification's `name` "
        f"rule (lowercase + digits + single hyphens, <=64 chars): {violations}"
    )


def test_every_shipped_skill_md_name_conforms_and_matches_its_directory() -> None:
    """Tier 1: every ``SKILL.md`` reyn ships -- builtin registry tier AND
    plugin tier -- declares a frontmatter ``name`` that satisfies the
    specification's grammar AND equals its own parent directory name (the
    specification requires the match)."""
    registry_paths = [Path(entry["path"]).resolve() for entry in BUILTIN_SKILLS.values()]
    plugin_paths = _plugin_skill_md_paths()
    assert len(registry_paths) >= 1, (
        "vacuity guard: BUILTIN_SKILLS enumerated zero SKILL.md paths"
    )
    assert len(plugin_paths) >= 1, (
        "vacuity guard: the builtin/plugins/*/skills/*/SKILL.md walk found "
        "nothing -- either no plugin ships a skill (unexpected: the rag "
        "plugin does) or the layout drifted from the glob"
    )

    grammar_violations: "dict[str, str]" = {}
    missing_name: "list[str]" = []
    directory_mismatch: "dict[str, str]" = {}
    for path in sorted(set(registry_paths) | set(plugin_paths)):
        name = _frontmatter_name(path)
        if name is None:
            missing_name.append(str(path))
            continue
        why = _nonconformance(name)
        if why is not None:
            grammar_violations[str(path)] = f"{name!r}: {why}"
        directory = path.parent.name
        if name != directory:
            directory_mismatch[str(path)] = (
                f"frontmatter name {name!r} != parent directory {directory!r}"
            )

    assert not missing_name, (
        "shipped SKILL.md declares no frontmatter `name` -- the install tools "
        f"read that key to prefill a skills.yaml entry: {missing_name}"
    )
    assert not grammar_violations, (
        "shipped SKILL.md `name` violates the Agent Skills specification's "
        f"grammar: {grammar_violations}"
    )
    assert not directory_mismatch, (
        "the Agent Skills specification requires `name` to match the parent "
        f"directory name: {directory_mismatch}"
    )


def test_builtin_registry_key_agrees_with_its_skill_md_frontmatter_name() -> None:
    """Tier 1: a builtin skill has one spelling, not two -- the
    ``BUILTIN_SKILLS`` key (what invocation resolves) and the ``SKILL.md``
    frontmatter ``name`` (what the install tools read, and what the
    specification ties to the directory) must agree, so renaming one without
    the other cannot ship."""
    disagreements = {}
    for key, entry in BUILTIN_SKILLS.items():
        frontmatter_name = _frontmatter_name(Path(entry["path"]).resolve())
        if frontmatter_name != key:
            disagreements[key] = f"SKILL.md frontmatter name is {frontmatter_name!r}"
    assert not disagreements, (
        "BUILTIN_SKILLS key disagrees with its SKILL.md frontmatter `name`: "
        f"{disagreements}"
    )
