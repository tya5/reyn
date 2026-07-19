"""Tier 2: OS invariant — ADR 0064 plugin model P2 (plugin_install / plugin_uninstall).

Tests:
  1. e2e install + uninstall roundtrip: a real local plugin dir (skills
     capability) is copied to ~/.reyn/plugins/<name>/, registered into
     .reyn/config/skills.yaml (tagged plugin_id), then plugin_uninstall
     removes both the registry entry and the global copy.
  2. enforcement (real PermissionResolver, no approval): the global-copy
     write is denied — demonstrates the gate is load-bearing, not decorative
     (CLAUDE.md: gate strip-falsify, real resolver not None).
  2b. #3088: the mcp register's OWN require_file_write gate on mcp.yaml
     (distinct from the global-copy gate) — denied without approval for that
     path (nothing written), approved → registered + mcp_server_installed
     audit event fires.
  3. reconcile: a plugin dir left with an _install_state.json marker AND a
     mid-register registry entry (a simulated crash between register and
     completion) is rolled back — BOTH the registry entry and the copy — by
     the next reconcile pass (drop-registry-first, §3.11).
  4. name-collision precedence (§3.8): a `local` install refuses to shadow
     an already-installed `builtin`-sourced plugin of the same name.
  5. run-code trust gate (§3.10 item 3, security core): a {kind:git} install
     requires a per-install operator-trust decision that a persistent
     http.get / web.fetch approval does NOT satisfy. Strip it → PermissionError,
     nothing fetched/written. Operator YES → proceeds; operator NO → denied.
  6. network-free spawn (§3.11): a plugin with a requirements.txt + an mcp
     capability materialises a per-plugin venv at install time; the registered
     mcp spawn command points at that venv's interpreter (no spawn-time fetch).
  7. pypi dep-fetch gate: the dep-materialisation network fetch is gated by
     require_http_get(pypi.org) — approved → materialises; stripped → denied.

Real PermissionResolver + OpContext + a real RequestBus-compatible Fake
(scriptable answers) throughout (no mocks). HOME is monkeypatched per-test so
~/.reyn/plugins/ never touches the real home dir.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import (
    _write_install_state,
    plugins_root,
    reconcile_plugin_installs,
)
from reyn.core.op_runtime.plugin_install import (
    handle as install_handle,
)
from reyn.core.op_runtime.plugin_uninstall import handle as uninstall_handle
from reyn.intervention_choices import NO, YES
from reyn.schemas.models import PluginInstallIROp, PluginUninstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ── shared stubs (real API surface, no mocks) ─────────────────────────────────


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    """Minimal real-callable event log stub — records emitted calls (for
    audit-event witnessing) without any other side effect."""
    subscribers: list = []

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class _FakeBus:
    """Real RequestBus-compatible Fake that pre-answers with a scripted choice
    (same pattern as tests/test_require_file_jit_ask_1505.py's _FakeBus — a
    scriptable Fake, not a mock: it implements the real ``request`` surface)."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _make_git_plugin_repo(base: Path, name: str = "gitplugin") -> Path:
    """A real local git repo containing a minimal plugin (skills capability),
    usable as a file:// {kind:git} source."""
    repo = base / "repo"
    (repo / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (repo / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "git plugin",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    (repo / "skills" / "hi").mkdir(parents=True, exist_ok=True)
    (repo / "skills" / "hi" / "SKILL.md").write_text(
        "---\nname: hi\ndescription: from git\n---\n\nBody.\n", encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _make_plugin_source(base: Path, name: str = "myplugin") -> Path:
    """A minimal local plugin dir: manifest + one skills capability."""
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
    return plugin_dir


def _make_ctx(
    tmp_path: Path,
    *,
    approve_plugins_root: bool = True,
    approve_mcp_yaml: bool = True,
    interactive: bool = False,
    bus: object | None = None,
    approve_all_http: bool = False,
) -> OpContext:
    """Build a real OpContext with a PermissionResolver. When
    ``approve_plugins_root`` is True, session-approves ~/.reyn/plugins/
    (recursive) + the three registry config files — the granted-path
    baseline every non-enforcement test needs. The enforcement test passes
    False to demonstrate the gate actually denies without it.

    ``approve_mcp_yaml`` (default True, independent of ``approve_plugins_root``)
    controls whether ``.reyn/config/mcp.yaml`` specifically is session-approved
    — the mcp register's OWN require_file_write gate (#3088), distinct from the
    global-copy write gate on ``~/.reyn/plugins/``. A test demonstrating THAT
    gate is load-bearing passes False here while leaving
    ``approve_plugins_root`` True (the copy proceeds; the mcp registration
    write is what gets denied).

    ``approve_all_http`` session-approves the http.get wildcard host — this is
    the FETCH axis (git clone / pypi reachability). The run-code trust gate is
    a SEPARATE axis, so approving this must NOT let a {kind:git} install run
    without the run-code prompt (test 5 asserts exactly that). ``interactive``
    + ``bus`` drive the run-code trust prompt (which never persists)."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=interactive,
    )
    if approve_plugins_root:
        resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
        for cfg in ("pipelines.yaml", "skills.yaml"):
            resolver.session_approve_path(
                str(project_root / ".reyn" / "config" / cfg), "test", "file.write",
            )
    if approve_mcp_yaml:
        resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / "mcp.yaml"), "test", "file.write",
        )
    if approve_all_http:
        # http.get is EXACT-host-matched at the gate (a "*" session approval
        # does not cover a specific host), so approve the concrete host the
        # dep-materialisation fetch uses. This is the FETCH axis only — it must
        # not (and does not) satisfy the run-code trust gate (test 5).
        resolver.session_approve_host("*", "test")
        resolver.session_approve_host("pypi.org", "test")

    decl = PermissionDecl(
        file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
        http_get=[{"host": "*"}],
    )
    return OpContext(
        workspace=_StubWorkspace(base_dir=project_root),
        events=_Events(),
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=bus,
    )


# ── Test 1: e2e install + uninstall roundtrip ─────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_install_uninstall_roundtrip(tmp_path, monkeypatch):
    """Tier 2: a real plugin_install op copies the plugin to ~/.reyn/plugins/<name>/,
    registers its skills capability into .reyn/config/skills.yaml (tagged
    plugin_id), and plugin_uninstall removes both. RED if the copy, the
    registry entry, the plugin_id tag, or the uninstall's removal is missing."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_plugin_source(tmp_path / "src")
    ctx = _make_ctx(tmp_path)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    assert result["name"] == "myplugin"
    assert result["capabilities"] == ["skills"]

    plugin_root = plugins_root() / "myplugin"
    assert plugin_root.is_dir(), "plugin was not copied to ~/.reyn/plugins/<name>/"
    assert (plugin_root / "skills" / "hello" / "SKILL.md").exists()

    skills_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "skills.yaml"
    raw = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))
    entry = raw["skills"]["entries"]["hello"]
    assert entry["plugin_id"] == "myplugin", "registered entry missing plugin_id provenance (§3.7)"

    # ── uninstall ──
    uop = PluginUninstallIROp(kind="plugin_uninstall", name="myplugin")
    uresult = await uninstall_handle(uop, ctx)

    assert uresult["status"] == "uninstalled"
    assert uresult["removed"]["skills"] == ["hello"]
    assert uresult["copy_removed"] is True
    assert not plugin_root.exists(), "plugin copy was not removed by uninstall"

    raw_after = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))
    assert raw_after["skills"]["entries"] == {}, "registry entry survived uninstall"


# ── Test 2: enforcement (real resolver, gate strip-falsify) ──────────────────


@pytest.mark.asyncio
async def test_plugin_install_denied_without_write_approval(tmp_path, monkeypatch):
    """Tier 2: security-critical gate — WITHOUT an approval/JIT-ask grant for
    ~/.reyn/plugins/, a real PermissionResolver denies the global-copy write
    (require_file_write's decl-less "zone OR approved" invariant: a mere
    PermissionDecl declaration does not itself grant). RED if plugin_install
    writes the global copy despite no approval — the exact unauthorized-write
    this gate exists to prevent (ADR 0064 §3.10 item 1)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_plugin_source(tmp_path / "src", name="unapproved-plugin")
    ctx = _make_ctx(tmp_path, approve_plugins_root=False)

    op = PluginInstallIROp(
        kind="plugin_install", source={"kind": "local", "path": str(source)},
    )
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)

    assert not (plugins_root() / "unapproved-plugin").exists(), (
        "plugin copy was written despite a denied permission gate"
    )


# ── Test 2b: mcp register's OWN write gate (#3088) ────────────────────────────


def _make_mcp_plugin_source(base: Path, name: str = "mcpplugin") -> Path:
    """A minimal local plugin dir: manifest + one mcp capability (no
    requirements.txt — offline, no ``uv`` dependency)."""
    plugin_dir = base / name
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": name, "version": "0.1.0", "description": "mcp test plugin",
            "capabilities": [{"kind": "mcp"}],
        }),
        encoding="utf-8",
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"srv": {"command": "python", "args": ["-m", "srv"]}}}),
        encoding="utf-8",
    )
    return plugin_dir


@pytest.mark.asyncio
async def test_register_mcp_denied_without_mcp_yaml_write_approval(tmp_path, monkeypatch):
    """Tier 2: security-critical gate — #3088. ``_register_mcp`` writes
    ``.reyn/config/mcp.yaml`` via its OWN ``require_file_write`` gate, DISTINCT
    from the global-copy write gate on ``~/.reyn/plugins/`` (which IS approved
    here). Without a grant for mcp.yaml specifically, a real PermissionResolver
    denies the mcp registration write. RED if plugin_install writes mcp.yaml
    despite no approval for that path — the exact asymmetric ungated write
    #3088 reports (sibling skill/pipeline registers already gated their own
    config write; mcp's register did not)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_mcp_plugin_source(tmp_path / "src")
    ctx = _make_ctx(tmp_path, approve_plugins_root=True, approve_mcp_yaml=False)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)

    mcp_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    assert not mcp_yaml.exists(), (
        "mcp.yaml was written despite a denied permission gate on that path"
    )
    # The global copy DID proceed (that gate was granted) — demonstrating the
    # mcp.yaml denial is this OP's own gate, not a knock-on of the copy gate.
    assert (plugins_root() / "mcpplugin").is_dir()


@pytest.mark.asyncio
async def test_register_mcp_gate_allows_and_emits_audit_event(tmp_path, monkeypatch):
    """Tier 2: with mcp.yaml write approved, the register proceeds — the entry
    lands in mcp.yaml (tagged plugin_id) AND the ``mcp_server_installed`` audit
    event fires through ``ctx.events``. Complements the denial test above:
    together they show the new gate blocks when unapproved and does not
    regress the approved (existing-behavior) path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = _make_mcp_plugin_source(tmp_path / "src")
    ctx = _make_ctx(tmp_path)  # approve_mcp_yaml defaults True

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"

    mcp_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    servers = yaml.safe_load(mcp_yaml.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert servers["srv"]["plugin_id"] == "mcpplugin"

    audit_event_names = [call[0][0] for call in ctx.events.calls if call[0]]
    assert "mcp_server_installed" in audit_event_names, (
        "mcp registration succeeded but emitted no mcp_server_installed audit event"
    )


# ── Test 3: reconcile rolls back a crashed partial install ───────────────────


@pytest.mark.asyncio
async def test_reconcile_rolls_back_partial_install_registry_and_copy(tmp_path, monkeypatch):
    """Tier 2: reconcile (§3.11) rolls back a mid-REGISTER partial — BOTH the
    dangling registry entry AND the copy, drop-registry-first.

    Simulates a crash AFTER a capability was registered (a skills.yaml entry
    tagged plugin_id) but BEFORE plugin_install_completed (the marker still
    present). RED if reconcile removes only the copy and leaves the registry
    entry — a dangling skill whose path no longer exists (the exact
    co-vet-flagged gap: 'reconcile が partial copy を rmtree するが mid-register が
    書いた registry entry を drop していない')."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project_root = tmp_path / "proj"
    project_root.mkdir()

    # A partial copy (marker present = never completed) that DID register a skill.
    partial = plugins_root() / "crashed-plugin"
    (partial / "skills" / "s").mkdir(parents=True)
    _write_install_state(partial, "local")

    skills_yaml = project_root / ".reyn" / "config" / "skills.yaml"
    skills_yaml.parent.mkdir(parents=True, exist_ok=True)
    skills_yaml.write_text(
        yaml.dump({"skills": {"entries": {
            "orphan": {"path": str(partial / "skills" / "s"), "plugin_id": "crashed-plugin", "enabled": True},
            "kept": {"path": "/somewhere/else", "enabled": True},  # NOT this plugin — must survive
        }}}),
        encoding="utf-8",
    )

    # A completed install (no marker) — reconcile must NOT touch it.
    completed = plugins_root() / "completed-plugin"
    completed.mkdir(parents=True)
    (completed / "content.txt").write_text("ok", encoding="utf-8")

    rolled_back = await reconcile_plugin_installs(
        plugins_root(), project_root=project_root, state_log=None, events=_Events(),
    )

    assert rolled_back == ["crashed-plugin"]
    assert not partial.exists(), "the crashed partial copy was not rolled back"
    assert completed.exists(), "reconcile incorrectly removed a completed install"

    after = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))["skills"]["entries"]
    assert "orphan" not in after, "reconcile left a dangling registry entry (registry-drop missing)"
    assert "kept" in after, "reconcile dropped an unrelated registry entry"


def test_reconcile_bare_sweep_without_project_root_drops_only_copies(tmp_path, monkeypatch):
    """Tier 2: reconcile with no project_root (a bare filesystem sweep — the
    standalone/CLI path) removes the partial copy but touches no registry
    (there is none in scope). RED if it errors or removes a completed copy."""
    import asyncio

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    partial = plugins_root() / "crashed"
    partial.mkdir(parents=True)
    _write_install_state(partial, "git")
    completed = plugins_root() / "done"
    completed.mkdir(parents=True)

    rolled_back = asyncio.run(reconcile_plugin_installs(plugins_root()))
    assert rolled_back == ["crashed"]
    assert not partial.exists()
    assert completed.exists()


# ── Test 4: name-collision precedence (§3.8) ──────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_install_refuses_lower_trust_shadow(tmp_path, monkeypatch):
    """Tier 2: a `local`-sourced install refuses to shadow an already
    -installed `builtin`-sourced plugin of the SAME name (ADR 0064 §3.8: the
    lower-trust-risk source never silently shadows a higher-trust-risk one).
    RED if the local re-install silently overwrites the builtin copy."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Simulate a builtin plugin registered under src/reyn/builtin/plugins/.
    builtin_src_root = tmp_path / "builtin_plugins"
    builtin_source = _make_plugin_source(builtin_src_root, name="shared-name")
    monkeypatch.setattr(
        "reyn.core.op_runtime.plugin_install._builtin_plugin_dir",
        lambda name: builtin_src_root / name,
    )

    ctx = _make_ctx(tmp_path)
    builtin_op = PluginInstallIROp(kind="plugin_install", source={"kind": "builtin", "name": "shared-name"})
    builtin_result = await install_handle(builtin_op, ctx)
    assert builtin_result["status"] == "installed", f"builtin install failed: {builtin_result}"

    local_source = _make_plugin_source(tmp_path / "local_src", name="shared-name")
    local_op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(local_source)})
    local_result = await install_handle(local_op, ctx)

    assert local_result["status"] == "skipped", f"expected skipped, got {local_result}"
    assert "shared-name" in local_result["error"]


# ── Test 5: run-code trust gate (§3.10 item 3 — the RCE boundary) ────────────


@pytest.mark.asyncio
async def test_git_install_denied_when_run_code_trust_not_granted(tmp_path, monkeypatch):
    """Tier 2: SECURITY CORE — a {kind:git} install is denied when the operator
    has NOT granted per-install run-code trust — EVEN THOUGH the http.get /
    web.fetch host is fully approved. This is the co-vet BLOCK: fetch approval
    (per-host, persistent, web.fetch-shared) must NEVER satisfy the run-code
    axis, or an approved-once host becomes silent-RCE for every future git
    plugin. Strip the run-code grant (non-interactive → the gate cannot be
    satisfied) and assert PermissionError + NOTHING cloned/written.

    RED if require_http_get alone lets the git install proceed (the exact
    conflation the run-code gate closes)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo = _make_git_plugin_repo(tmp_path)
    # http.get approved for ALL hosts (fetch axis fully granted), but the
    # resolver is NON-interactive so the run-code trust gate cannot be granted.
    ctx = _make_ctx(tmp_path, approve_all_http=True, interactive=False, bus=None)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "git", "url": repo.as_uri()})
    with pytest.raises(PermissionError) as exc:
        await install_handle(op, ctx)
    assert "run" in str(exc.value).lower(), "deny message should name the run-code trust boundary"

    installed = [p for p in plugins_root().glob("*") if not p.name.startswith(".")]
    assert installed == [], f"git plugin was installed despite no run-code trust: {installed}"
    # No dangling staging clone either.
    staging = plugins_root() / ".staging"
    assert not staging.exists() or not any(staging.iterdir()), "a clone happened before the run-code gate"


@pytest.mark.asyncio
async def test_git_install_run_code_trust_yes_proceeds(tmp_path, monkeypatch):
    """Tier 2: with an interactive operator who answers YES to the run-code
    trust prompt, a {kind:git} install proceeds (the gate is a real decision
    point, not an always-deny). Proves the deny above is the gate firing, not a
    blanket non-interactive refusal of everything."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo = _make_git_plugin_repo(tmp_path)
    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, approve_all_http=True, interactive=True, bus=bus)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "git", "url": repo.as_uri()})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"YES answer did not install: {result}"
    # The run-code trust prompt actually fired (distinct kind), and it is the
    # git-run-code gate specifically.
    assert any(iv.kind == "permission.plugin_git_run_code_trust" for iv in bus.asks), (
        "the run-code trust prompt did not fire for a {kind:git} install"
    )
    assert (plugins_root() / "gitplugin").is_dir()


@pytest.mark.asyncio
async def test_git_install_run_code_trust_no_denies(tmp_path, monkeypatch):
    """Tier 2: an interactive operator who answers NO to the run-code trust
    prompt gets a PermissionError; nothing is installed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo = _make_git_plugin_repo(tmp_path)
    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, approve_all_http=True, interactive=True, bus=bus)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "git", "url": repo.as_uri()})
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)
    assert not (plugins_root() / "gitplugin").exists()


def test_run_code_trust_choices_offer_no_persist_option():
    """Tier 1: the run-code trust choice set is yes/no ONLY — no ALWAYS/NEVER.
    The non-persistence of the run-code decision (§3.10 'never auto-run') is a
    STRUCTURAL property (the UI cannot present a persist option), not a
    resolver convention. RED if a persist choice is ever added."""
    from reyn.intervention_choices import (
        ALWAYS,
        NEVER,
        plugin_run_code_trust_choices,
    )

    ids = {c.id for c in plugin_run_code_trust_choices()}
    assert ids == {YES, NO}, f"run-code trust choices must be yes/no only, got {ids}"
    assert ALWAYS not in ids and NEVER not in ids, "run-code trust must NOT offer a persist option"


# ── Test 6: network-free spawn (materialise → venv interpreter, §3.11) ────────


def _uv_available() -> bool:
    import shutil as _sh
    return _sh.which("uv") is not None


@pytest.mark.skipif(not _uv_available(), reason="uv not on PATH")
@pytest.mark.asyncio
async def test_materialise_deps_rewrites_mcp_spawn_to_venv_interpreter(tmp_path, monkeypatch):
    """Tier 2: §3.11 headline property — a plugin with a requirements.txt + an
    mcp capability (command: python) materialises a per-plugin venv at INSTALL
    time, and the registered mcp spawn command is rewritten to that venv's
    interpreter — so spawn needs no network (no `uv run --with` at spawn).

    Uses an EMPTY requirements.txt so `uv venv` + `uv pip install -r` run fully
    offline (no package fetch) yet still exercise the real materialise +
    command-rewrite path. RED if the registered command stays 'python' (spawn
    would then depend on ambient env / fetch)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Build a plugin: manifest(mcp) + root .mcp.json(command:python) + empty reqs.
    src = tmp_path / "src" / "venvplugin"
    (src / ".reyn-plugin").mkdir(parents=True)
    (src / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "venvplugin", "version": "0.1.0", "capabilities": [{"kind": "mcp"}]}),
        encoding="utf-8",
    )
    (src / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"srv": {"command": "python", "args": ["-m", "srv"]}}}),
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text("", encoding="utf-8")  # empty → offline materialise

    ctx = _make_ctx(tmp_path, approve_all_http=True)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"

    venv_python = plugins_root() / "venvplugin" / ".venv" / "bin" / "python"
    assert venv_python.exists(), "per-plugin venv interpreter was not materialised at install time"

    mcp_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    servers = yaml.safe_load(mcp_yaml.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert servers["srv"]["command"] == str(venv_python), (
        "registered mcp spawn command was not rewritten to the venv interpreter "
        "(spawn would not be network-free)"
    )
    assert servers["srv"]["plugin_id"] == "venvplugin"


# ── Test 7: pypi dep-fetch gate (require_http_get on the package index) ───────


@pytest.mark.asyncio
async def test_dep_materialise_denied_when_pypi_http_get_stripped(tmp_path, monkeypatch):
    """Tier 2: the dep-materialisation network fetch is gated by
    require_http_get(pypi.org). Strip that grant (non-interactive, http.get NOT
    approved) → a plugin carrying a requirements.txt is denied BEFORE any uv
    fetch, and no venv is created. RED if materialise fetches without the gate.

    Note: plugins_root() IS approved here (so the copy succeeds and we reach the
    materialise step), isolating the pypi gate as the sole denier."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = tmp_path / "src" / "needsdeps"
    (src / ".reyn-plugin").mkdir(parents=True)
    (src / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "needsdeps", "version": "0.1.0", "capabilities": []}),
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text("somepkg==1.0\n", encoding="utf-8")

    # plugins_root approved (copy allowed) but http.get NOT approved + non-interactive.
    ctx = _make_ctx(tmp_path, approve_plugins_root=True, approve_all_http=False, interactive=False)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    # The pypi require_http_get gate raises PermissionError (propagated by the
    # handler, same as every other gate — execute_op turns it into status=denied
    # in production). The venv must NOT have been created (no fetch ran).
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)
    venv = plugins_root() / "needsdeps" / ".venv"
    assert not venv.exists(), "a venv/fetch happened despite the pypi http.get gate being stripped"


@pytest.mark.skipif(not _uv_available(), reason="uv not on PATH")
@pytest.mark.asyncio
async def test_dep_materialise_proceeds_when_pypi_http_get_approved(tmp_path, monkeypatch):
    """Tier 2: with the pypi http.get grant present, dep-materialisation
    proceeds (empty requirements.txt → offline). Complements the strip-falsify
    above: the gate is a real allow/deny point, not an always-deny."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = tmp_path / "src" / "okdeps"
    (src / ".reyn-plugin").mkdir(parents=True)
    (src / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "okdeps", "version": "0.1.0", "capabilities": []}),
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text("", encoding="utf-8")

    ctx = _make_ctx(tmp_path, approve_plugins_root=True, approve_all_http=True)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"approved-pypi materialise failed: {result}"
    assert (plugins_root() / "okdeps" / ".venv" / "bin" / "python").exists()
