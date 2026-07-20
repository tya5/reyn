"""Tier 1/2: skill-load invocation-time variable expansion (ADR 0064 §3.5,
plugin-model P4, #3070) + the #3196 provenance gate on that expansion.

Pins (real instances throughout — no mocks):

  1. ``load_skill_body`` expands ``${REYN_PLUGIN_ROOT}``/``${REYN_SKILL_DIR}``/
     ``${REYN_PROJECT_DIR}`` to real, DISTINCT non-default filesystem paths,
     and returns ``(body, env_tokens_expanded)`` (Tier 1, ``reyn.plugins.skill_load``).
  2. ``resolve_plugin_root`` walks up from a skill dir to a real
     ``.reyn-plugin/plugin.json`` written to disk, and returns a DIFFERENT
     value than ``skill_dir`` when one exists — falls back to ``skill_dir``
     itself when none does (no collapse in either direction).
  3. ``${CLAUDE_*}`` aliases expand to the SAME value as their canonical
     ``${REYN_*}`` counterpart (§3.6), reusing P1's ``PluginTokenContext``.
  4. ``${env:VAR}`` expands from a real (non-default) ``os.environ`` value;
     an UNSET ``${env:VAR}`` is left untouched (not blanked); a bare
     ``${SOME_VAR}`` (no ``env:`` prefix) is left untouched even when
     ``SOME_VAR`` IS set — proving the namespaced syntax doesn't fall back
     to ``expand_env``'s bare-``${VAR}`` behaviour.
  5. #3196 provenance gate (Tier 2b, OS invariant — security): the real
     ``file`` read op (``file.handle``) expands a ``SKILL.md``-named file
     ONLY when its resolved path ALSO falls into a registered provenance
     class (config-registered entry here; builtin/plugin are covered by
     ``test_reactive_orchestration_plugins_references_reachable_3162.py`` /
     the plugin_install integration test below). An UNREGISTERED
     ``SKILL.md`` — project root, nested subdir, deep-nested, an
     unregistered would-be plugin root — is read byte-identical, with NO
     ``skill_body_loaded`` event, in every placement (negative witness,
     multiple). A symlink / ``..``-relative path is judged on its
     RESOLVED target, not its literal text (closes the judged-face /
     read-face mismatch the firm design calls out). The audit-event
     carries ``provenance`` + ``env_tokens_expanded`` but never the
     expanded VALUE.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.skills.registry import SkillEntry
from reyn.data.workspace.workspace import Workspace
from reyn.plugins.skill_load import (
    is_skill_body_path,
    load_skill_body,
    resolve_plugin_root,
)
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _run(coro):
    return asyncio.run(coro)


# ── is_skill_body_path ───────────────────────────────────────────────────────

def test_is_skill_body_path_matches_only_skill_md_basename(tmp_path):
    """Tier 1: routes on the SKILL.md basename only, not directory naming."""
    assert is_skill_body_path(tmp_path / "some-skill" / "SKILL.md")
    assert not is_skill_body_path(tmp_path / "SKILL.md.bak")
    assert not is_skill_body_path(tmp_path / "some-skill" / "reference.md")
    assert not is_skill_body_path(tmp_path / "skill.md")  # case-sensitive


# ── resolve_plugin_root ──────────────────────────────────────────────────────

def test_resolve_plugin_root_finds_manifest_walking_up(tmp_path):
    """Tier 1: a real .reyn-plugin/plugin.json above the skill dir is found,
    and the returned root is a DIFFERENT path than skill_dir itself."""
    plugin_dir = tmp_path / "my-plugin"
    manifest_dir = plugin_dir / ".reyn-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
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
    manifest_dir = plugin_dir / ".reyn-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
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

    expanded, env_count = load_skill_body(content, skill_path=skill_path, project_dir=project_dir)

    assert f"root={plugin_dir.resolve()}" in expanded
    assert f"skill={skill_dir.resolve()}" in expanded
    assert f"project={project_dir.resolve()}" in expanded
    # all three resolve to genuinely distinct values -- no collapse (§3.4/§3.6)
    assert len({str(plugin_dir.resolve()), str(skill_dir.resolve()), str(project_dir.resolve())}) == 3
    assert env_count == 0  # no ${env:...} tokens in this content


def test_load_skill_body_claude_alias_matches_reyn_token_value(tmp_path):
    """Tier 1: §3.6 -- ${CLAUDE_*} expands to the SAME value as its
    canonical ${REYN_*} counterpart, reusing P1's PluginTokenContext."""
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    content = "${CLAUDE_SKILL_DIR}|${REYN_SKILL_DIR}"
    expanded, _env_count = load_skill_body(
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

    expanded, _env_count = load_skill_body(
        "${CLAUDE_SKILL_DIR}", skill_path=skill_path, project_dir=project_dir,
        alias_claude=False,
    )

    assert expanded == "${CLAUDE_SKILL_DIR}"


# ── load_skill_body: ${env:VAR} ──────────────────────────────────────────────

def test_load_skill_body_expands_env_token_from_real_environ(tmp_path, monkeypatch):
    """Tier 1: ${env:VAR} expands from a real, non-default os.environ value."""
    monkeypatch.setenv("REYN_SKILL_LOAD_TEST_TOKEN", "quetzal-9182")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, env_count = load_skill_body(
        "value=${env:REYN_SKILL_LOAD_TEST_TOKEN}",
        skill_path=skill_path, project_dir=project_dir,
    )

    assert expanded == "value=quetzal-9182"
    assert env_count == 1


def test_load_skill_body_unset_env_token_left_untouched(tmp_path, monkeypatch):
    """Tier 1: an UNSET ${env:VAR} is left as a literal token, never blanked."""
    monkeypatch.delenv("REYN_SKILL_LOAD_TEST_UNSET_TOKEN", raising=False)
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, env_count = load_skill_body(
        "${env:REYN_SKILL_LOAD_TEST_UNSET_TOKEN}",
        skill_path=skill_path, project_dir=project_dir,
    )

    assert expanded == "${env:REYN_SKILL_LOAD_TEST_UNSET_TOKEN}"
    assert env_count == 0


def test_load_skill_body_bare_var_not_expanded_even_when_set(tmp_path, monkeypatch):
    """Tier 1: a bare ${VAR} (no env: prefix) is left untouched even though
    the same-named env var IS set -- proves skill-load does NOT fall back to
    expand_env's bare-${VAR} syntax (collision-avoidance is the whole point,
    see module docstring)."""
    monkeypatch.setenv("SOME_VAR", "should-not-appear")
    skill_dir = tmp_path / "standalone-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "some-project"
    project_dir.mkdir()

    expanded, env_count = load_skill_body(
        "example: ${SOME_VAR}", skill_path=skill_path, project_dir=project_dir,
    )

    assert expanded == "example: ${SOME_VAR}"
    assert "should-not-appear" not in expanded
    assert env_count == 0


# ── Integration: the real file read op + the #3196 provenance gate ──────────

# A synthetic, harmless sentinel -- NEVER a real credential (per the
# standing "never inspect a real secret value" discipline). Proves the
# value reaches `content` on a TRUSTED read and never reaches it (or an
# audit-event) on an UNTRUSTED one.
_SENTINEL_ENV_VAR = "REYN_SKILL_LOAD_GATE_TEST_SENTINEL"
_SENTINEL_ENV_VALUE = "FAKE_SECRET_VALUE_3196"
_SENTINEL_BODY = f"---\nname: probe\n---\nsecret=${{env:{_SENTINEL_ENV_VAR}}}\n"


def _make_ctx(project_root: Path, *, available_skills=None) -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test_skill_load",
        available_skills=available_skills,
    )
    return ctx, events


def _assert_not_expanded(result: dict, events: EventLog, monkeypatch) -> None:
    """Shared negative-witness assertion: the RAW token survives (never
    blanked, never resolved), and no skill_body_loaded event fires."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    assert result["status"] == "ok", result
    assert f"secret=${{env:{_SENTINEL_ENV_VAR}}}" in result["content"], (
        "unregistered SKILL.md must be read byte-identical -- token must "
        "survive UNEXPANDED"
    )
    assert _SENTINEL_ENV_VALUE not in result["content"]
    assert not [e for e in events.all() if e.type == "skill_body_loaded"]


@pytest.mark.parametrize(
    "rel_dir",
    [
        "",  # project root itself
        "some/subdir",  # one level nested
        "a/b/c/deeply/nested/dir",  # deep nesting
    ],
)
def test_file_read_op_does_not_expand_unregistered_skill_md(tmp_path, monkeypatch, rel_dir):
    """Tier 2: (security, #3196; falsify, multiple placements) an
    UNREGISTERED `SKILL.md` -- no config entry, not builtin, not a
    registered plugin body -- is read byte-identical regardless of WHERE
    under the project root it sits. This is the exact confused-deputy
    reproduction: filename alone must no longer be sufficient."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = (project_root / rel_dir) if rel_dir else project_root
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    ctx, events = _make_ctx(project_root)  # available_skills=None -- nothing registered

    result = _run(handle(FileIROp(kind="file", op="read", path=rel_path), ctx))

    _assert_not_expanded(result, events, monkeypatch)


def test_file_read_op_does_not_expand_unregistered_plugin_root(tmp_path, monkeypatch):
    """Tier 2: (security, #3196; falsify) a `SKILL.md` sitting under a
    would-be plugin directory that was NEVER completed through
    `plugin_install` (no `.reyn-plugin/_source_kind.json` completion
    sidecar) is NOT treated as plugin-provenance -- `read_plugin_body_bytes`
    itself already gates on `is_registered_plugin_root`, this proves that
    holds end-to-end through the `file` op too."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fake_plugin_dir = home / ".reyn" / "plugins" / "not-really-installed"
    (fake_plugin_dir / "skills" / "x").mkdir(parents=True)
    (fake_plugin_dir / ".reyn-plugin").mkdir()
    (fake_plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "not-really-installed", "version": "1.0.0"}),
        encoding="utf-8",
    )  # a hand-placed marker, no completion sidecar -- must NOT count
    skill_path = fake_plugin_dir / "skills" / "x" / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")

    project_root = tmp_path / "project-root-real"
    project_root.mkdir()
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    # Grant read on the fake plugin dir itself -- this test's subject is the
    # PROVENANCE check (unregistered -> no expansion), not the separate
    # read-zone gate a registered plugin body would also bypass.
    resolver.session_approve_path(str(fake_plugin_dir), "test_skill_load", "file.read", recursive=True)
    ws = Workspace(
        events=events, base_dir=project_root,
        permission_resolver=resolver, actor="test_skill_load",
    )
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test_skill_load",
    )

    result = _run(handle(FileIROp(kind="file", op="read", path=str(skill_path)), ctx))

    _assert_not_expanded(result, events, monkeypatch)


def test_file_read_op_expands_config_registered_skill_md(tmp_path, monkeypatch):
    """Tier 2: (security, #3196 positive/regression witness) a
    `SKILL.md` DECLARED via `skills.entries` (mirrored here as a
    `SkillEntry` on `ctx.available_skills`, the SAME registry `:skill`
    invocation resolves against) still expands exactly as before --
    proving the gate closes the hole WITHOUT breaking the registered
    case. Also pins the `skill_body_loaded` event's `provenance` +
    `env_tokens_expanded` fields, and that NEITHER the event NOR anything
    else carries the expanded secret VALUE outside `content` itself."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry])

    result = _run(handle(FileIROp(kind="file", op="read", path=rel_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret={_SENTINEL_ENV_VALUE}" in result["content"]

    skill_load_events = [e for e in events.all() if e.type == "skill_body_loaded"]
    assert skill_load_events, "expected a skill_body_loaded audit-event to be emitted"
    event = next(e for e in skill_load_events if e.data["path"] == rel_path)
    assert event.data["provenance"] == "config_entry"
    assert event.data["env_tokens_expanded"] == 1
    # The value must NEVER appear in the audit-event -- an audit-event is
    # not a second secret-storage location (#3196 firm design).
    assert _SENTINEL_ENV_VALUE not in json.dumps(event.data)


def test_file_read_op_symlink_judged_by_resolved_target_not_literal_path(tmp_path, monkeypatch):
    """Tier 2: (security, #3196) the judged face and the read face must be
    the SAME resolved path (firm design point 2). A symlink named
    `SKILL.md` living OUTSIDE any registered location, but pointing AT a
    real registered skill's body file, is still recognized as trusted --
    proving the gate resolves the path before comparing, rather than
    comparing `op.path`'s own (unregistered) location."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    real_skill_dir = project_root / "skills" / "greeter"
    real_skill_dir.mkdir(parents=True)
    real_skill_path = real_skill_dir / "SKILL.md"
    real_skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_real_path = str(real_skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_real_path)

    # An UNREGISTERED location, elsewhere under the project root, whose
    # SKILL.md is a symlink to the REGISTERED body above.
    unregistered_dir = project_root / "unregistered" / "elsewhere"
    unregistered_dir.mkdir(parents=True)
    symlink_path = unregistered_dir / "SKILL.md"
    symlink_path.symlink_to(real_skill_path)
    rel_symlink_path = str(symlink_path.relative_to(project_root))

    ctx, events = _make_ctx(project_root, available_skills=[entry])

    result = _run(handle(FileIROp(kind="file", op="read", path=rel_symlink_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret={_SENTINEL_ENV_VALUE}" in result["content"]
    skill_load_events = [e for e in events.all() if e.type == "skill_body_loaded"]
    assert any(e.data["provenance"] == "config_entry" for e in skill_load_events)


def test_file_read_op_dotdot_path_judged_by_resolved_target(tmp_path, monkeypatch):
    """Tier 2: (security, #3196) a `..`-relative `op.path` that resolves to
    a REGISTERED body still expands -- proves comparison happens on the
    `.resolve()`d path, not the literal (un-normalized) path string."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry])

    dotdot_path = "skills/other-skill-name/../greeter/SKILL.md"

    result = _run(handle(FileIROp(kind="file", op="read", path=dotdot_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret={_SENTINEL_ENV_VALUE}" in result["content"]
    skill_load_events = [e for e in events.all() if e.type == "skill_body_loaded"]
    assert any(e.data["provenance"] == "config_entry" for e in skill_load_events)


def test_file_read_op_does_not_expand_non_skill_md_file(tmp_path):
    """Tier 2: (falsify) the SAME token text in a differently-named file is
    returned VERBATIM -- proves expansion is keyed on the SKILL.md filename,
    not on content that merely looks like a token."""
    project_root = tmp_path / "project-root-real"
    project_root.mkdir()
    other_path = project_root / "notes.md"
    other_path.write_text("Project: ${REYN_PROJECT_DIR}\n", encoding="utf-8")
    ctx, events = _make_ctx(project_root)

    result = _run(handle(FileIROp(kind="file", op="read", path="notes.md"), ctx))

    assert result["status"] == "ok", result
    assert "Project: ${REYN_PROJECT_DIR}" in result["content"]
    assert not [e for e in events.all() if e.type == "skill_body_loaded"]


# ── Integration: a real plugin_install (P2) copy feeding skill-load (P4) ────
#
# P2's `plugin_install` bakes ${REYN_PLUGIN_ROOT} into a plugin's SKILL.md
# files at COPY time (`_expand_plugin_files`, `reyn.core.op_runtime.
# plugin_install`) but deliberately leaves ${REYN_SKILL_DIR}/${REYN_PROJECT_DIR}
# unbaked (see that module's docstring, updated by #3070) -- this test proves
# BOTH halves of that split with one real end-to-end run: install a real local
# plugin, then read its installed SKILL.md through the real file read op.


def test_plugin_install_bakes_plugin_root_skill_load_resolves_the_rest(tmp_path, monkeypatch):
    """Tier 2: OS invariant -- ${REYN_PLUGIN_ROOT} is ALREADY a literal path
    in the file plugin_install (P2) copied to disk (baked at copy time, no
    skill-load pass needed to see it); ${REYN_SKILL_DIR} and
    ${REYN_PROJECT_DIR} are STILL literal ${...} tokens in that same copied
    file until the real file read op's skill-load pass (P4) expands them to
    the REAL, current project root -- proving the split is not merely
    documented but actually holds for a real plugin_install output."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    from reyn.core.op_runtime.plugin_install import handle as install_handle
    from reyn.core.op_runtime.plugin_install import plugins_root
    from reyn.schemas.models import PluginInstallIROp

    source = tmp_path / "src-plugin"
    (source / ".reyn-plugin").mkdir(parents=True)
    (source / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": "loadertest", "version": "0.1.0", "description": "d",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (source / "skills" / "greeter").mkdir(parents=True)
    (source / "skills" / "greeter" / "SKILL.md").write_text(
        "---\nname: greeter\n---\n"
        "root=${REYN_PLUGIN_ROOT} skill=${REYN_SKILL_DIR} project=${REYN_PROJECT_DIR}\n",
        encoding="utf-8",
    )

    project_root = tmp_path / "operator-project"
    project_root.mkdir()
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
    resolver.session_approve_path(str(plugins_root()), "test", "file.read", recursive=True)
    for cfg in ("mcp.yaml", "pipelines.yaml", "skills.yaml"):
        resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / cfg), "test", "file.write",
        )
    events = EventLog()
    ws = Workspace(
        events=events, base_dir=project_root,
        permission_resolver=resolver, actor="test",
    )
    install_ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(
            file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
        ),
        permission_resolver=resolver,
        actor="test",
    )

    install_op = PluginInstallIROp(
        kind="plugin_install", source={"kind": "local", "path": str(source)},
    )
    install_result = _run(install_handle(install_op, install_ctx))
    assert install_result["status"] == "installed", install_result

    plugin_root = plugins_root() / "loadertest"
    skill_path = plugin_root / "skills" / "greeter" / "SKILL.md"

    # Pin the copy-time half directly: ${REYN_PLUGIN_ROOT} is ALREADY a
    # literal path on disk, before any file read op runs.
    on_disk = skill_path.read_text(encoding="utf-8")
    assert f"root={plugin_root.resolve()}" in on_disk
    assert "${REYN_SKILL_DIR}" in on_disk
    assert "${REYN_PROJECT_DIR}" in on_disk

    # Now read it through the real file op -- skill-load resolves the rest.
    read_result = _run(handle(FileIROp(kind="file", op="read", path=str(skill_path)), install_ctx))
    assert read_result["status"] == "ok", read_result
    content = read_result["content"]
    assert f"root={plugin_root.resolve()}" in content
    assert f"skill={skill_path.parent.resolve()}" in content
    assert f"project={project_root.resolve()}" in content
    assert "${REYN_SKILL_DIR}" not in content
    # #3196: a REGISTERED (install-completed) plugin body's provenance is
    # reported as "plugin" -- positive witness for that provenance class.
    plugin_events = [e for e in events.all() if e.type == "skill_body_loaded"]
    assert any(e.data.get("provenance") == "plugin" for e in plugin_events)
    assert "${REYN_PROJECT_DIR}" not in content
