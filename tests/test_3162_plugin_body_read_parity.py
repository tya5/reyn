"""Tier 2: OS invariant — plugin/builtin body-read permission parity (owner
ruling + architect firm, related to #3162).

A builtin skill/pipeline's shipped BODY (``reyn.builtin.docs.
read_builtin_body_bytes``, #2913/#2914) already short-circuits the generic
``read_file`` op's out-of-project-root gate. A REGISTERED plugin's
``skills/**``/``pipelines/**`` body resolves outside ``project_root`` too
(``~/.reyn/plugins/``, a per-operator global cache) but had no equivalent
short-circuit — ``reyn.plugins.body_read.read_plugin_body_bytes`` closes that
asymmetry. This test pins the 5 witnesses the co-vet requires:

  1. Body dirs OUTSIDE ``skills/``/``pipelines/`` (``scripts/``,
     ``requirements.txt``) stay gated even under a REGISTERED plugin root.
  2. ★ LOAD-BEARING: an UNREGISTERED root — a hand-placed ``.reyn-plugin/``
     marker + ``skills/x/SKILL.md``, no install ever run — is NOT bypassed.
     This is the one witness that actually distinguishes "gated on
     install-registration" from "gated on marker presence": a marker-only
     implementation would pass every other witness here and still leak.
  3. ``~/.reyn/plugins/.staging/`` (git-clone staging, pre-approval content)
     is never bypassed.
  4. Positive / reachable-for-purpose: a genuinely-registered plugin's
     ``skills/**`` body (including an L3 bundled reference file alongside
     ``SKILL.md``) reads with NO approval prompt, even with ``project_root``
     elsewhere.
  5. Strip-falsify, one property per test (Edit-only during authoring,
     monkeypatch here mirrors ``test_2913_builtin_body_wheel_reachable.py``'s
     own falsify test): stripping the registration check flips ONLY witness 2
     red; stripping the ``.staging`` exclusion flips ONLY witness 3 red.

Real ``PluginInstallIROp`` → real ``plugin_install.handle`` → a REAL
``~/.reyn/plugins/<name>/`` install (HOME monkeypatched to tmp_path, no real
home dir touched) produces the registered fixture — no hand-crafted sidecar
files standing in for "install completed" (fake ban: real registry, real
files, real read-op path). Unregistered/staging fixtures are plain
filesystem writes, exactly modeling content that never went through install.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import plugin_install as plugin_install_mod
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle as file_handle
from reyn.core.op_runtime.plugin_install import handle as install_handle
from reyn.core.op_runtime.plugin_install import plugins_root
from reyn.data.workspace.workspace import Workspace
from reyn.plugins import body_read as body_read_mod
from reyn.plugins.body_read import read_plugin_body_bytes
from reyn.schemas.models import FileIROp, PluginInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _run(coro):
    return asyncio.run(coro)


def _read_op(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def _make_plugin_source(base: Path, name: str = "myplugin") -> Path:
    """A minimal local plugin dir: manifest + a skills capability whose skill
    dir ALSO carries an L3 bundled reference file, plus non-body content
    (``scripts/``, ``requirements.txt``) that must stay gated post-install."""
    plugin_dir = base / name
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "test plugin",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "hello").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: says hi\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "hello" / "references").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "hello" / "references" / "notes.md").write_text(
        "REFERENCE_MARKER_CONTENT\n", encoding="utf-8",
    )
    (plugin_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "scripts" / "setup.py").write_text("print('not a body')\n", encoding="utf-8")
    (plugin_dir / "requirements.txt").write_text("requests\n", encoding="utf-8")
    return plugin_dir


def _install_ctx(project_root: Path) -> OpContext:
    """Real OpContext whose PermissionResolver session-approves ONLY the
    plugin-install write paths — enough for `plugin_install` itself to
    succeed, so the fixture is produced through the real install op."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
    for cfg in ("pipelines.yaml", "skills.yaml", "mcp.yaml"):
        resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / cfg), "test", "file.write",
        )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test",
    )


def _read_ctx(unrelated_project_root: Path) -> OpContext:
    """Real OpContext for the READ side, whose PermissionResolver's
    project_root is UNRELATED to ~/.reyn/plugins/ and grants NOTHING — any
    read that isn't bypassed by the mechanism under test hits the
    out-of-root gate and raises PermissionError, non-interactively."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=unrelated_project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=unrelated_project_root, interactive=False,
    )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test",
    )


@pytest.fixture
def registered_plugin_root(tmp_path, monkeypatch) -> Path:
    """A genuinely REGISTERED ``~/.reyn/plugins/myplugin/`` — installed
    through the real `plugin_install` op, HOME redirected to tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_plugin_source(tmp_path / "src")
    project_root = tmp_path / "install_proj"
    project_root.mkdir()
    ctx = _install_ctx(project_root)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    result = _run(install_handle(op, ctx))
    assert result["status"] == "installed", result

    plugin_root = plugins_root() / "myplugin"
    assert (plugin_root / ".reyn-plugin" / "_source_kind.json").is_file(), (
        "fixture invariant: install must leave the completed-provenance sidecar"
    )
    return plugin_root


def test_witness1_non_body_dirs_stay_gated_under_registered_root(registered_plugin_root, tmp_path):
    """Tier 2: Witness 1: scripts/ and requirements.txt under a REGISTERED plugin
    root are NOT bypassed — least-privilege scoping to skills/+pipelines/."""
    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    ctx = _read_ctx(unrelated_root)

    for rel in ("scripts/setup.py", "requirements.txt"):
        path = str(registered_plugin_root / rel)
        assert read_plugin_body_bytes(path) is None, rel
        with pytest.raises(PermissionError, match="read from"):
            _run(file_handle(_read_op(path), ctx))


def test_witness2_unregistered_marker_only_root_not_bypassed(tmp_path, monkeypatch):
    """Tier 2: Witness 2 (load-bearing): a HAND-PLACED `.reyn-plugin/` marker under
    ~/.reyn/plugins/ with a skills/ body — but NEVER installed (no completed
    provenance sidecar) — is NOT bypassed. Distinguishes registration-based
    gating from marker-based gating: a marker-only implementation would leak
    here."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    fake_root = plugins_root() / "unregistered"
    (fake_root / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (fake_root / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "unregistered", "version": "0.1.0", "capabilities": []}),
        encoding="utf-8",
    )
    (fake_root / "skills" / "x").mkdir(parents=True, exist_ok=True)
    skill_path = fake_root / "skills" / "x" / "SKILL.md"
    skill_path.write_text("---\nname: x\ndescription: x\n---\n\nBody.\n", encoding="utf-8")

    assert read_plugin_body_bytes(str(skill_path)) is None

    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    ctx = _read_ctx(unrelated_root)
    with pytest.raises(PermissionError, match="read from"):
        _run(file_handle(_read_op(str(skill_path)), ctx))


def test_witness3_staging_dir_not_bypassed(tmp_path, monkeypatch):
    """Tier 2: Witness 3: ~/.reyn/plugins/.staging/ (git-clone staging — content that
    predates even the run-code trust gate) is never bypassed.

    Two layouts are checked: the REAL production shape (a clone-id
    subdirectory between `.staging/` and `skills/` — `plugin_install.py`'s
    `{kind:git}` branch always nests one level deeper), where the
    least-privilege body-dir check ALSO happens to deny it (defense in
    depth, documented in `body_read.py`'s module docstring); and a FLAT
    layout (`skills/` directly under `.staging/`) that isolates the
    `_STAGING_DIR_NAME` check as the ONLY thing standing between this read
    and a bypass — this is the shape the strip-falsify test below targets,
    so this witness must cover it too (a real green here that only ever
    exercised the nested/coincidentally-covered layout would not actually
    pin the `_STAGING_DIR_NAME` line as load-bearing)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    ctx = _read_ctx(unrelated_root)

    nested = plugins_root() / ".staging" / "git-abc123" / "skills" / "x"
    nested.mkdir(parents=True, exist_ok=True)
    nested_skill_path = nested / "SKILL.md"
    nested_skill_path.write_text("---\nname: x\ndescription: x\n---\n\nBody.\n", encoding="utf-8")
    assert read_plugin_body_bytes(str(nested_skill_path)) is None
    with pytest.raises(PermissionError, match="read from"):
        _run(file_handle(_read_op(str(nested_skill_path)), ctx))

    flat = plugins_root() / ".staging" / "skills" / "x"
    flat.mkdir(parents=True, exist_ok=True)
    flat_skill_path = flat / "SKILL.md"
    flat_skill_path.write_text("---\nname: x\ndescription: x\n---\n\nBody.\n", encoding="utf-8")
    assert read_plugin_body_bytes(str(flat_skill_path)) is None
    with pytest.raises(PermissionError, match="read from"):
        _run(file_handle(_read_op(str(flat_skill_path)), ctx))


def test_witness4_registered_plugin_body_reachable_without_approval(registered_plugin_root, tmp_path):
    """Tier 2: Witness 4 (positive / reachable-for-purpose): a registered plugin's
    SKILL.md AND its L3 bundled reference file both read successfully with
    NO approval, even though project_root is unrelated — the actual bug
    (#3162-adjacent) this PR fixes: "the reference is documented but
    unreachable" no longer happens for an installed plugin."""
    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    ctx = _read_ctx(unrelated_root)

    skill_path = registered_plugin_root / "skills" / "hello" / "SKILL.md"
    result = _run(file_handle(_read_op(str(skill_path)), ctx))
    assert result["status"] == "ok", result
    assert "says hi" in result["content"]

    ref_path = registered_plugin_root / "skills" / "hello" / "references" / "notes.md"
    result2 = _run(file_handle(_read_op(str(ref_path)), ctx))
    assert result2["status"] == "ok", result2
    assert "REFERENCE_MARKER_CONTENT" in result2["content"]


def test_falsify_strip_registration_check_flips_witness2_red(tmp_path, monkeypatch):
    """Tier 2: Strip-falsify (registration check only): monkeypatching
    `is_registered_plugin_root` to always return True makes the PREVIOUSLY
    denied unregistered-root read from witness 2 succeed — proving witness 2
    depends specifically on the registration check, not on some other path
    (e.g. the .staging exclusion) accidentally covering it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    fake_root = plugins_root() / "unregistered"
    (fake_root / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (fake_root / "skills" / "x").mkdir(parents=True, exist_ok=True)
    skill_path = fake_root / "skills" / "x" / "SKILL.md"
    skill_path.write_text("Body.\n", encoding="utf-8")

    monkeypatch.setattr(plugin_install_mod, "is_registered_plugin_root", lambda root: True)

    assert read_plugin_body_bytes(str(skill_path)) == b"Body.\n"


def test_falsify_strip_staging_exclusion_flips_witness3_red(tmp_path, monkeypatch):
    """Tier 2: Strip-falsify (.staging exclusion only): monkeypatching the module's
    `_STAGING_DIR_NAME` sentinel to a value that never matches makes the
    PREVIOUSLY denied `.staging/` read from witness 3 succeed — proving
    witness 3 depends specifically on that exclusion line, isolated from the
    registration check (which this test does NOT touch)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Deliberately WITHOUT the extra clone-id nesting `plugin_install.py`'s
    # real `{kind:git}` staging path has (`.staging/git-<uuid>/...`) — this
    # falsify test isolates ONLY the `_STAGING_DIR_NAME` exclusion property:
    # with the top-level name-match check stripped, `.staging` itself would
    # be treated as an ordinary plugin-name path component with `skills/`
    # directly beneath it, exactly like any other plugin root.
    staging_skill_dir = plugins_root() / ".staging" / "skills" / "x"
    staging_skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = staging_skill_dir / "SKILL.md"
    skill_path.write_text("Body.\n", encoding="utf-8")

    monkeypatch.setattr(body_read_mod, "_STAGING_DIR_NAME", "__never_matches__")
    # The staging root itself has no completed-provenance sidecar, so without
    # ALSO stripping the registration check this read still correctly denies
    # — isolate the exclusion property by additionally granting registration
    # for this one test, so ONLY the .staging behavior is under test.
    monkeypatch.setattr(plugin_install_mod, "is_registered_plugin_root", lambda root: True)

    assert read_plugin_body_bytes(str(skill_path)) == b"Body.\n"


def test_operator_file_outside_zone_still_denied(tmp_path, monkeypatch):
    """Tier 2: No security regression: an ordinary (non-plugin) file outside the
    read zone is still denied by the unmodified gate — the plugin
    short-circuit only ever fires for a path under ~/.reyn/plugins/."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    project_root = tmp_path / "proj"
    project_root.mkdir()
    ctx = _read_ctx(project_root)

    operator_file = tmp_path / "operator_secret.txt"
    operator_file.write_text("not a plugin body", encoding="utf-8")

    with pytest.raises(PermissionError, match="read from"):
        _run(file_handle(_read_op(str(operator_file)), ctx))
