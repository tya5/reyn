"""Tier 1: skill-load invocation-time variable expansion — the PURE-FUNCTION
half (ADR 0064 §3.5, plugin-model P4, #3070).

Pins (real instances throughout — no mocks):

  1. ``load_skill_body`` expands ``${REYN_PLUGIN_ROOT}``/``${REYN_SKILL_DIR}``/
     ``${REYN_PROJECT_DIR}`` to real, DISTINCT non-default filesystem paths,
     and returns ``(body, env_names_expanded, env_names_denied)`` (Tier 1,
     ``reyn.plugins.skill_load``).
  2. ``resolve_plugin_root`` walks up from a skill dir to a real
     ``plugin.json`` written to disk (plugin root — #4570 conversion A),
     and returns a DIFFERENT
     value than ``skill_dir`` when one exists — falls back to ``skill_dir``
     itself when none does (no collapse in either direction).
  3. ``${CLAUDE_*}`` aliases expand to the SAME value as their canonical
     ``${REYN_*}`` counterpart (§3.6), reusing P1's ``PluginTokenContext``.
     These are LOCATION tokens, unaffected by the #3198 allowlist gate.
  4. ``${env:VAR}`` expands from a real (non-default) ``os.environ`` value
     ONLY when the name is on the caller's ``permission_decl.env_expand``
     allowlist (#3198, deny-by-default — an OMITTED ``permission_decl``
     denies everything); an UNSET (but allowlisted) ``${env:VAR}`` is left
     untouched (not blanked); a bare ``${SOME_VAR}`` (no ``env:`` prefix) is
     left untouched even when ``SOME_VAR`` IS set — proving the namespaced
     syntax doesn't fall back to ``expand_env``'s bare-``${VAR}`` behaviour.

**FP-0066 P0 (#3247) split.** The INTEGRATION half of this module — the real
`file` read op / `FileIROp` tests exercising the #3196 provenance gate + the
#3198 allowlist gate end-to-end — moved to
``tests/core/test_op_runtime_load_skill_3247.py``, now constructing `LoadSkillIROp`
and calling `reyn.core.op_runtime.load_skill.handle` directly (the
responsibility moved out of `file.py` into the dedicated `load_skill` op;
that test module also pins the file.read strip-falsify: a `file.read` of a
SKILL.md path — even a registered one — is now a plain, unexpanded read).
The pure functions tested here (`load_skill_body` / `is_skill_body_path` /
`resolve_plugin_root`) did not change signature or behavior — only WHO
calls them changed.
"""
from __future__ import annotations

import json

import pytest

from reyn.plugins.skill_load import (
    is_skill_body_path,
    load_skill_body,
    resolve_plugin_root,
)
from reyn.security.permissions.permissions import PermissionDecl

# ── is_skill_body_path ───────────────────────────────────────────────────────

def test_is_skill_body_path_matches_only_skill_md_basename(tmp_path):
    """Tier 1: routes on the SKILL.md basename only, not directory naming."""
    assert is_skill_body_path(tmp_path / "some-skill" / "SKILL.md")
    assert not is_skill_body_path(tmp_path / "SKILL.md.bak")
    assert not is_skill_body_path(tmp_path / "some-skill" / "reference.md")
    assert not is_skill_body_path(tmp_path / "skill.md")  # case-sensitive


# ── resolve_plugin_root ──────────────────────────────────────────────────────

def test_resolve_plugin_root_finds_manifest_walking_up(tmp_path):
    """Tier 1: a real plugin.json above the skill dir is found, and the
    returned root is a DIFFERENT path than skill_dir itself."""
    plugin_dir = tmp_path / "my-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "my-plugin", "version": "1.0.0"}), encoding="utf-8",
    )
    skill_dir = plugin_dir / "skills" / "rag-search"
    skill_dir.mkdir(parents=True)

    root = resolve_plugin_root(skill_dir)

    assert root == plugin_dir.resolve()
    assert root != skill_dir.resolve()


def test_resolve_plugin_root_falls_back_to_skill_dir_when_no_manifest(tmp_path):
    """Tier 1: a standalone (non-plugin) skill has no manifest above it —
    resolve_plugin_root falls back to skill_dir itself."""
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()

    root = resolve_plugin_root(skill_dir)

    assert root == skill_dir.resolve()


# ── load_skill_body: REYN_* tokens ───────────────────────────────────────────

def test_load_skill_body_expands_reyn_tokens_to_distinct_real_paths(tmp_path):
    """Tier 1: PLUGIN_ROOT / SKILL_DIR / PROJECT_DIR each expand to their
    own real, non-default, DISTINCT filesystem path."""
    plugin_dir = tmp_path / "acme-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "acme-plugin", "version": "2.3.4"}), encoding="utf-8",
    )
    skill_dir = plugin_dir / "skills" / "widget-maker"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("body", encoding="utf-8")
    project_dir = tmp_path / "operator-project-xyz"
    project_dir.mkdir()

    content = (
        "root=${REYN_PLUGIN_ROOT} skill=${REYN_SKILL_DIR} "
        "project=${REYN_PROJECT_DIR}"
    )

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        content, skill_path=skill_path, project_dir=project_dir,
    )

    assert f"root={plugin_dir.resolve()}" in expanded
    assert f"skill={skill_dir.resolve()}" in expanded
    assert f"project={project_dir.resolve()}" in expanded
    # all three resolve to genuinely distinct values -- no collapse (§3.4/§3.6)
    assert len({str(plugin_dir.resolve()), str(skill_dir.resolve()), str(project_dir.resolve())}) == 3
    # no ${env:...} tokens in this content -- location tokens are unaffected
    # by the #3198 allowlist gate regardless of permission_decl (omitted here).
    assert env_expanded == []
    assert env_denied == []


def test_load_skill_body_claude_alias_matches_reyn_token_value(tmp_path):
    """Tier 1: §3.6 -- ${CLAUDE_*} expands to the SAME value as its
    canonical ${REYN_*} counterpart, reusing P1's PluginTokenContext."""
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    content = "${CLAUDE_SKILL_DIR}|${REYN_SKILL_DIR}"
    expanded, _persisted, _loc_map, _env_expanded, _env_denied = load_skill_body(
        content, skill_path=skill_path, project_dir=project_dir, alias_claude=True,
    )

    claude_val, reyn_val = expanded.split("|")
    assert claude_val == reyn_val == str(skill_dir.resolve())


def test_load_skill_body_claude_alias_off_leaves_token_untouched(tmp_path):
    """Tier 1: with alias_claude=False (the default), a ${CLAUDE_*} token is
    left as a literal, unexpanded string."""
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, _env_expanded, _env_denied = load_skill_body(
        "${CLAUDE_SKILL_DIR}", skill_path=skill_path, project_dir=project_dir,
        alias_claude=False,
    )

    assert expanded == "${CLAUDE_SKILL_DIR}"


# ── load_skill_body: ${env:VAR} + the #3198 allowlist gate ───────────────────

def test_load_skill_body_expands_env_token_when_allowlisted(tmp_path, monkeypatch):
    """Tier 1: ${env:VAR} expands from a real, non-default os.environ value
    WHEN the name is declared on permission_decl.env_expand (#3198)."""
    monkeypatch.setenv("REYN_SKILL_LOAD_TEST_TOKEN", "quetzal-9182")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        "value=${env:REYN_SKILL_LOAD_TEST_TOKEN}",
        skill_path=skill_path, project_dir=project_dir,
        permission_decl=PermissionDecl(env_expand=["REYN_SKILL_LOAD_TEST_TOKEN"]),
    )

    assert expanded == "value=quetzal-9182"
    assert env_expanded == ["REYN_SKILL_LOAD_TEST_TOKEN"]
    assert env_denied == []


def test_load_skill_body_env_token_denied_by_default_empty_allowlist(tmp_path, monkeypatch):
    """Tier 2: (security, #3198 core witness) with NO permission_decl passed
    (the deny-by-default path every existing/future caller gets for free),
    a SET ${env:VAR} is NOT expanded -- the real environ value never reaches
    the output, and the token survives verbatim (never blanked)."""
    monkeypatch.setenv("REYN_SKILL_LOAD_TEST_TOKEN", "quetzal-9182")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        "value=${env:REYN_SKILL_LOAD_TEST_TOKEN}",
        skill_path=skill_path, project_dir=project_dir,
        # permission_decl omitted entirely -- the default-deny path.
    )

    assert expanded == "value=${env:REYN_SKILL_LOAD_TEST_TOKEN}"
    assert "quetzal-9182" not in expanded
    assert env_expanded == []
    assert env_denied == ["REYN_SKILL_LOAD_TEST_TOKEN"]


@pytest.mark.parametrize(
    "var_name", ["REYN_SKILL_LOAD_TEST_TOKEN", "SOME_OTHER_CREDENTIAL_NAME"],
)
def test_load_skill_body_env_token_denied_by_default_multiple_names(tmp_path, monkeypatch, var_name):
    """Tier 2: (security, #3198) the default-empty-allowlist denial holds for
    MULTIPLE distinct variable names, not just one hand-picked example."""
    monkeypatch.setenv(var_name, "should-never-appear")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, _env_denied = load_skill_body(
        f"value=${{env:{var_name}}}", skill_path=skill_path, project_dir=project_dir,
    )

    assert expanded == f"value=${{env:{var_name}}}"
    assert "should-never-appear" not in expanded
    assert env_expanded == []


def test_load_skill_body_env_allowlist_is_selective(tmp_path, monkeypatch):
    """Tier 2: (security, #3198 selective witness) a body with TWO
    ${env:VAR} tokens, only ONE of which is allowlisted -- the allowlisted
    one expands, the other stays a literal unexpanded token (never blanked
    to empty string). Proves the gate is neither "block everything" nor
    "allow everything"."""
    monkeypatch.setenv("REYN_SKILL_LOAD_ALLOWED_VAR", "allowed-value-777")
    monkeypatch.setenv("REYN_SKILL_LOAD_DENIED_VAR", "denied-value-888")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        "a=${env:REYN_SKILL_LOAD_ALLOWED_VAR} b=${env:REYN_SKILL_LOAD_DENIED_VAR}",
        skill_path=skill_path, project_dir=project_dir,
        permission_decl=PermissionDecl(env_expand=["REYN_SKILL_LOAD_ALLOWED_VAR"]),
    )

    assert expanded == "a=allowed-value-777 b=${env:REYN_SKILL_LOAD_DENIED_VAR}"
    assert "denied-value-888" not in expanded
    assert env_expanded == ["REYN_SKILL_LOAD_ALLOWED_VAR"]
    assert env_denied == ["REYN_SKILL_LOAD_DENIED_VAR"]


def test_load_skill_body_env_allowlist_wildcard_expands_any_name(tmp_path, monkeypatch):
    """Tier 1: the "*" wildcard (mirroring secret_write's shape) allows ANY
    declared-at-runtime name, not just an explicitly-named one."""
    monkeypatch.setenv("REYN_SKILL_LOAD_WILDCARD_VAR", "wildcard-value-555")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        "v=${env:REYN_SKILL_LOAD_WILDCARD_VAR}",
        skill_path=skill_path, project_dir=project_dir,
        permission_decl=PermissionDecl(env_expand=["*"]),
    )

    assert expanded == "v=wildcard-value-555"
    assert env_expanded == ["REYN_SKILL_LOAD_WILDCARD_VAR"]
    assert env_denied == []


def test_load_skill_body_unset_allowlisted_env_token_left_untouched(tmp_path, monkeypatch):
    """Tier 1: an UNSET ${env:VAR} is left as a literal token, never blanked
    -- even when the name IS allowlisted (unset vs. undeclared are BOTH
    "leave alone", but only "undeclared" counts as a denial)."""
    monkeypatch.delenv("REYN_SKILL_LOAD_TEST_UNSET_TOKEN", raising=False)
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        "${env:REYN_SKILL_LOAD_TEST_UNSET_TOKEN}",
        skill_path=skill_path, project_dir=project_dir,
        permission_decl=PermissionDecl(env_expand=["REYN_SKILL_LOAD_TEST_UNSET_TOKEN"]),
    )

    assert expanded == "${env:REYN_SKILL_LOAD_TEST_UNSET_TOKEN}"
    assert env_expanded == []
    assert env_denied == []  # allowlisted, just unset -- not a denial


def test_load_skill_body_bare_var_not_expanded_even_when_set(tmp_path, monkeypatch):
    """Tier 1: a bare ${VAR} (no env: prefix) is left untouched even though
    the same-named env var IS set -- proves skill-load does NOT fall back to
    expand_env's bare-${VAR} syntax (collision-avoidance is the whole point,
    see module docstring). Unaffected by the #3198 allowlist -- there is no
    ${env:...} token here for the gate to even see."""
    monkeypatch.setenv("SOME_VAR", "should-not-appear")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, _persisted, _loc_map, env_expanded, env_denied = load_skill_body(
        "example: ${SOME_VAR}", skill_path=skill_path, project_dir=project_dir,
        permission_decl=PermissionDecl(env_expand=["*"]),
    )

    assert expanded == "example: ${SOME_VAR}"
    assert "should-not-appear" not in expanded
    assert env_expanded == []
    assert env_denied == []

