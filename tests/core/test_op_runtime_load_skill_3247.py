"""Tier 2: the dedicated `load_skill` op (FP-0066 P0, #3247) — the current
home for `file.handle` + `FileIROp` SKILL.md integration coverage
(#3196/#3198 provenance + allowlist witnesses): these tests construct
`LoadSkillIROp` and call `reyn.core.op_runtime.load_skill.handle` directly.
The pure-function tests (`load_skill_body` / `is_skill_body_path` /
`resolve_plugin_root`) live in `tests/plugins/test_skill_load.py` — a
separate module, unaffected by which op handles the SKILL.md-path case.

Also pins the STRIP-FALSIFY half of `load_skill`'s design (#3247): a `file.read` of
a `SKILL.md` path — even a REGISTERED one — is now a PLAIN read, byte-
identical, no expansion, no `skill_body_loaded` event. This is the structural
proof that the responsibility actually LEFT `file.py`, not just that
`load_skill` gained it.

No mocks — real Workspace / PermissionResolver / EventLog throughout.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle as file_handle
from reyn.core.op_runtime.load_skill import handle as load_skill_handle
from reyn.data.skills.registry import SkillEntry
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp, LoadSkillIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import collect_events


def _run(coro):
    return asyncio.run(coro)


_SENTINEL_ENV_VAR = "REYN_LOAD_SKILL_GATE_TEST_SENTINEL"
_SENTINEL_ENV_VALUE = "FAKE_SECRET_VALUE_3196"
_SENTINEL_BODY = f"---\nname: probe\n---\nsecret=${{env:{_SENTINEL_ENV_VAR}}}\n"


def _make_ctx(
    project_root: Path, *, available_skills=None, env_expand=(_SENTINEL_ENV_VAR,),
) -> tuple[OpContext, EventLog]:
    """``env_expand`` defaults to allowlisting the SENTINEL var only -- these
    #3196-provenance-focused tests want to isolate "is this path trusted at
    all" from the #3198 allowlist question, so the allowlist is deliberately
    pre-granted for the ONE name they reference."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(env_expand=list(env_expand)),
        permission_resolver=resolver,
        actor="test_load_skill",
        available_skills=available_skills,
    )
    return ctx, events


def _assert_not_expanded(result: dict, collected: list, monkeypatch) -> None:
    """Shared negative-witness assertion: the RAW token survives (never
    blanked, never resolved), and no skill_body_loaded event fires."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    assert result["status"] == "ok", result
    assert f"secret=${{env:{_SENTINEL_ENV_VAR}}}" in result["content"], (
        "unregistered SKILL.md must be loaded byte-identical -- token must "
        "survive UNEXPANDED"
    )
    assert _SENTINEL_ENV_VALUE not in result["content"]
    assert not [e for e in collected if e.type == "skill_body_loaded"]


@pytest.mark.parametrize(
    "rel_dir",
    [
        "",  # project root itself
        "some/subdir",  # one level nested
        "a/b/c/deeply/nested/dir",  # deep nesting
    ],
)
def test_load_skill_does_not_expand_unregistered_skill_md(tmp_path, monkeypatch, rel_dir):
    """Tier 2: (security, #3196; falsify, multiple placements) an
    UNREGISTERED `SKILL.md` -- no config entry, not builtin, not a
    registered plugin body -- is loaded byte-identical regardless of WHERE
    under the project root it sits."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = (project_root / rel_dir) if rel_dir else project_root
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    ctx, events = _make_ctx(project_root)  # available_skills=None -- nothing registered
    collected = collect_events(events)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_path), ctx))

    _assert_not_expanded(result, collected, monkeypatch)


def test_load_skill_does_not_expand_unregistered_plugin_root(tmp_path, monkeypatch):
    """Tier 2: (security, #3196; falsify) a `SKILL.md` sitting under a
    would-be plugin directory that was NEVER completed through
    `plugin_install` (no completion sidecar) is NOT treated as
    plugin-provenance."""
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
    collected = collect_events(events)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    resolver.session_approve_path(str(fake_plugin_dir), "test_load_skill", "file.read", recursive=True)
    ws = Workspace(
        events=events, base_dir=project_root,
        permission_resolver=resolver, actor="test_load_skill",
    )
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test_load_skill",
    )

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=str(skill_path)), ctx))

    _assert_not_expanded(result, collected, monkeypatch)


def test_load_skill_expands_config_registered_skill_md(tmp_path, monkeypatch):
    """Tier 2: (security, #3196 positive/regression witness, #3198 allowlist
    positive witness) a `SKILL.md` DECLARED via `skills.entries` (mirrored
    here as a `SkillEntry` on `ctx.available_skills`) expands exactly as
    before the extraction, and the `skill_body_loaded` event's fields are
    pinned (never the expanded VALUE)."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry])
    collected = collect_events(events)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret={_SENTINEL_ENV_VALUE}" in result["content"]

    skill_load_events = [e for e in collected if e.type == "skill_body_loaded"]
    assert skill_load_events, "expected a skill_body_loaded audit-event to be emitted"
    event = next(e for e in skill_load_events if e.data["path"] == rel_path)
    assert event.data["provenance"] == "config_entry"
    assert event.data["env_tokens_expanded"] == 1
    assert event.data["env_names_expanded"] == [_SENTINEL_ENV_VAR]
    assert event.data["env_tokens_denied"] == 0
    assert event.data["env_names_denied"] == []
    assert _SENTINEL_ENV_VALUE not in json.dumps(event.data)


def test_load_skill_config_registered_skill_md_env_denied_by_default(tmp_path, monkeypatch):
    """Tier 2: (security, #3198 core witness) a REGISTERED skill body (clears
    the #3196 provenance gate) does NOT get its ${env:VAR} expanded when
    `ctx.permission_decl.env_expand` is empty."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry], env_expand=())
    collected = collect_events(events)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret=${{env:{_SENTINEL_ENV_VAR}}}" in result["content"]
    assert _SENTINEL_ENV_VALUE not in result["content"]

    skill_load_events = [e for e in collected if e.type == "skill_body_loaded"]
    event = next(e for e in skill_load_events if e.data["path"] == rel_path)
    assert event.data["provenance"] == "config_entry"  # provenance gate still passed
    assert event.data["env_tokens_expanded"] == 0
    assert event.data["env_names_expanded"] == []
    assert event.data["env_tokens_denied"] == 1
    assert event.data["env_names_denied"] == [_SENTINEL_ENV_VAR]
    assert _SENTINEL_ENV_VALUE not in json.dumps(event.data)


def test_load_skill_symlink_judged_by_resolved_target_not_literal_path(tmp_path, monkeypatch):
    """Tier 2: (security, #3196) the judged face and the read face must be
    the SAME resolved path. A symlink named `SKILL.md` living OUTSIDE any
    registered location, but pointing AT a real registered skill's body
    file, is still recognized as trusted."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    real_skill_dir = project_root / "skills" / "greeter"
    real_skill_dir.mkdir(parents=True)
    real_skill_path = real_skill_dir / "SKILL.md"
    real_skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_real_path = str(real_skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_real_path)

    unregistered_dir = project_root / "unregistered" / "elsewhere"
    unregistered_dir.mkdir(parents=True)
    symlink_path = unregistered_dir / "SKILL.md"
    symlink_path.symlink_to(real_skill_path)
    rel_symlink_path = str(symlink_path.relative_to(project_root))

    ctx, events = _make_ctx(project_root, available_skills=[entry])
    collected = collect_events(events)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_symlink_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret={_SENTINEL_ENV_VALUE}" in result["content"]
    skill_load_events = [e for e in collected if e.type == "skill_body_loaded"]
    assert any(e.data["provenance"] == "config_entry" for e in skill_load_events)


def test_load_skill_dotdot_path_judged_by_resolved_target(tmp_path, monkeypatch):
    """Tier 2: (security, #3196) a `..`-relative `op.path` that resolves to
    a REGISTERED body still expands."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry])
    collected = collect_events(events)

    dotdot_path = "skills/other-skill-name/../greeter/SKILL.md"

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=dotdot_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret={_SENTINEL_ENV_VALUE}" in result["content"]
    skill_load_events = [e for e in collected if e.type == "skill_body_loaded"]
    assert any(e.data["provenance"] == "config_entry" for e in skill_load_events)


def test_load_skill_resolves_path_exactly_once_per_call(tmp_path, monkeypatch):
    """Tier 2: (security, #3196 co-vet round 2 — decision/content split)
    `load_skill.handle` resolves `op.path` EXACTLY ONCE and reuses that
    single result for the permission gate, the builtin/plugin provenance
    check, the ACTUAL byte read, and the config-registered provenance
    decision + expansion.

    Scope, precisely: this closes the in-process SPLIT between the trust
    decision and the content read. It does NOT close a true concurrent-
    OS-process race between the `resolve()` syscall and the later read
    syscall (see `resolve_path_for_gate`'s own docstring).

    Spies on `reyn.core.op_runtime.load_skill.resolve_path_for_gate` (the
    ONE function that ever calls `.resolve()` for this op) and asserts it
    is called exactly once for a real, real-instance `SKILL.md` load."""
    import reyn.core.op_runtime.load_skill as load_skill_module

    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry])
    collected = collect_events(events)

    calls: list[str] = []
    real_resolve_path_for_gate = load_skill_module.resolve_path_for_gate

    def _counting_resolve_path_for_gate(ctx_arg, path_str):
        calls.append(path_str)
        return real_resolve_path_for_gate(ctx_arg, path_str)

    monkeypatch.setattr(load_skill_module, "resolve_path_for_gate", _counting_resolve_path_for_gate)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_path), ctx))

    assert result["status"] == "ok", result
    assert calls == [rel_path], (
        f"expected `op.path` to be resolved exactly ONCE (for {rel_path!r}) "
        f"for this load, got these resolve call(s): {calls!r}"
    )
    skill_load_events = [e for e in collected if e.type == "skill_body_loaded"]
    assert any(e.data["provenance"] == "config_entry" for e in skill_load_events)


def test_load_skill_does_not_expand_non_skill_md_file(tmp_path):
    """Tier 2: (falsify) the SAME token text in a differently-named file is
    returned VERBATIM -- proves expansion depends on REGISTRATION, not on
    content that merely looks like a token (`load_skill` has no
    filename-routing gate at all — every `path` argument is a candidate;
    an unregistered one, of ANY name, is simply not expanded)."""
    project_root = tmp_path / "project-root-real"
    project_root.mkdir()
    other_path = project_root / "notes.md"
    other_path.write_text("Project: ${REYN_PROJECT_DIR}\n", encoding="utf-8")
    ctx, events = _make_ctx(project_root)
    collected = collect_events(events)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path="notes.md"), ctx))

    assert result["status"] == "ok", result
    assert "Project: ${REYN_PROJECT_DIR}" in result["content"]
    assert not [e for e in collected if e.type == "skill_body_loaded"]


def test_plugin_install_bakes_plugin_root_load_skill_resolves_the_rest(tmp_path, monkeypatch):
    """Tier 2: OS invariant -- ${REYN_PLUGIN_ROOT} is ALREADY a literal path
    in the file plugin_install (P2) copied to disk (baked at copy time);
    ${REYN_SKILL_DIR} and ${REYN_PROJECT_DIR} are STILL literal ${...}
    tokens in that same copied file until the real `load_skill` op (P4)
    expands them to the REAL, current project root."""
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
    collected = collect_events(events)
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

    on_disk = skill_path.read_text(encoding="utf-8")
    assert f"root={plugin_root.resolve()}" in on_disk
    assert "${REYN_SKILL_DIR}" in on_disk
    assert "${REYN_PROJECT_DIR}" in on_disk

    load_result = _run(load_skill_handle(
        LoadSkillIROp(kind="load_skill", path=str(skill_path)), install_ctx,
    ))
    assert load_result["status"] == "ok", load_result
    content = load_result["content"]
    assert f"root={plugin_root.resolve()}" in content
    assert f"skill={skill_path.parent.resolve()}" in content
    assert f"project={project_root.resolve()}" in content
    assert "${REYN_SKILL_DIR}" not in content
    plugin_events = [e for e in collected if e.type == "skill_body_loaded"]
    assert any(e.data.get("provenance") == "plugin" for e in plugin_events)
    assert "${REYN_PROJECT_DIR}" not in content


# ── Strip-falsify: file.read no longer special-cases SKILL.md at all ────────


def test_file_read_never_expands_a_skill_md_even_when_registered(tmp_path, monkeypatch):
    """Tier 2: (strip-falsify, #3247) proves the responsibility actually LEFT
    `file.py` — a `file.read` of a REGISTERED skill's SKILL.md (the exact
    case `load_skill` DOES expand, above) is a PLAIN read: byte-identical,
    no expansion, no `skill_body_loaded` event. If this failed, `file.read`
    would still be carrying the skill special-case the extraction was
    supposed to remove."""
    monkeypatch.setenv(_SENTINEL_ENV_VAR, _SENTINEL_ENV_VALUE)
    project_root = tmp_path / "project-root-real"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SENTINEL_BODY, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx, events = _make_ctx(project_root, available_skills=[entry])
    collected = collect_events(events)

    result = _run(file_handle(FileIROp(kind="file", op="read", path=rel_path), ctx))

    assert result["status"] == "ok", result
    assert f"secret=${{env:{_SENTINEL_ENV_VAR}}}" in result["content"], (
        "file.read must return the skill body byte-identical -- the "
        "expansion pass moved to load_skill and must not run here"
    )
    assert _SENTINEL_ENV_VALUE not in result["content"]
    assert not [e for e in collected if e.type == "skill_body_loaded"], (
        "file.read must never emit skill_body_loaded -- that responsibility "
        "belongs exclusively to the load_skill op now"
    )


def test_file_module_carries_no_skill_load_import(tmp_path):
    """Tier 1: (strip-falsify, #3247) `reyn.core.op_runtime.file` no longer
    imports anything from `reyn.plugins.skill_load` — the clean-break
    surface: the import itself, not just the runtime behavior, must be
    gone."""
    import reyn.core.op_runtime.file as file_module

    assert not hasattr(file_module, "is_skill_body_path")
    assert not hasattr(file_module, "load_skill_body")
    assert not hasattr(file_module, "_config_registered_skill_body_provenance")


# ── #4431 (architect correction): truncated load_skill can resume ──────────
#
# load_skill shared file.py's read-bounding cap (control_ir_inline_cap) but
# had no `char_offset`/`next_offset` support at all — a truncated skill body
# told the model "the full body is on disk at <path>" with no way to
# actually continue reading it. Distinct from #4431 item ③ (say a cut
# happened) — load_skill already said so via `_truncated`; this is the
# OUTPUT-side gap ③ doesn't cover: saying it happened is not the same as
# being able to read the rest.


def test_load_skill_truncated_result_carries_a_resumable_offset(tmp_path):
    """Tier 2: an unbounded load_skill over the cap returns `next_offset` —
    the exact field a resuming `read_file(path=..., offset=next_offset)`
    call needs (mirrors file.py's own #1209/#2335 shape, the established
    precedent — no new field name invented)."""
    ctx, _events = _make_ctx(tmp_path)
    big = "".join(f"line {i} {'.' * 60}\n" for i in range(400))
    from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES
    assert len(big) > MAX_CONTROL_IR_RESULT_INLINE_BYTES  # ensure over the floor cap
    ctx.workspace.write_file("SKILL.md", big)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path="SKILL.md"), ctx))

    assert result["status"] == "truncated"
    assert result["_truncated"] is True
    assert isinstance(result["next_offset"], int) and result["next_offset"] > 0
    assert str(result["next_offset"]) in result["note"]
    assert "read_file" in result["note"]


def test_load_skill_resume_via_read_file_recovers_the_exact_remainder(tmp_path):
    """Tier 2: end-to-end — the note's own remedy actually works. Reading the
    same path via `read_file(offset=next_offset)` picks up EXACTLY where
    load_skill's truncated content left off (no gap, no duplicated line) —
    concatenating the two recovers the original body byte-for-byte. This is
    the "assert both the count and the content" discipline (#4437's own
    review note): a plausible-looking `next_offset` that off-by-ones would
    still pass a bare "field is present" check but fail this one."""
    ctx, _events = _make_ctx(tmp_path)
    # Sized to overflow the cap just enough for ONE truncation round (both
    # load_skill and read_file share the same window-derived cap) — a
    # bigger body would need this test to loop resume calls, which is a
    # real thing a caller can do but not what THIS assertion is pinning.
    big = "".join(f"line {i} {'.' * 60}\n" for i in range(130))
    from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES
    assert len(big) > MAX_CONTROL_IR_RESULT_INLINE_BYTES
    ctx.workspace.write_file("SKILL.md", big)

    first = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path="SKILL.md"), ctx))
    assert first["status"] == "truncated"

    rest = _run(file_handle(
        FileIROp(kind="file", op="read", path="SKILL.md", offset=first["next_offset"]), ctx,
    ))
    assert rest["status"] == "ok", (
        f"test body sized wrong — resume itself truncated again: {rest}"
    )
    assert first["content"] + rest["content"] == big


def test_load_skill_under_cap_is_unaffected(tmp_path):
    """Tier 2: accept-side twin — a small skill body is unchanged: no
    `next_offset`, no `_truncated`, `status="ok"` as before this fix."""
    ctx, _events = _make_ctx(tmp_path)
    small = "---\nname: probe\n---\nsmall body\n"
    ctx.workspace.write_file("SKILL.md", small)

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path="SKILL.md"), ctx))

    assert result["status"] == "ok"
    assert result["content"] == small
    assert "next_offset" not in result
    assert "_truncated" not in result
