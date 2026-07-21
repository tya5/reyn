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
  7. pypi dep-fetch gate (#3048): the dep-fetch approval for pypi.org is
     DERIVED from the install's own gate-1 write-approval (not a separate
     interactive prompt) — config-tier deny still blocks it; gate-1 itself
     being denied still blocks it (execution never reaches the derive).
  8. #3048 seal: require_http_get(host) with a bus wired but unanswered
     awaits indefinitely (confirmed root cause of the codeact-30s-budget
     kill — a never-answered prompt, not a slow download or exhaustion).
  9. #3048 fix, load-bearing: a full plugin_install with only gate-1
     approved + an unanswering bus completes without hanging and WITHOUT
     raising a separate pypi.org prompt (strip the derive → this times out).
  10. #3048 security witness: the derive is scoped to EXACTLY pypi.org —
     an unrelated host (evil.com) is still gated (not a blanket http.get
     grant — confused-deputy guard).

Real PermissionResolver + OpContext + a real RequestBus-compatible Fake
(scriptable answers) throughout (no mocks). HOME is monkeypatched per-test so
~/.reyn/plugins/ never touches the real home dir.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import (
    _build_mcp_entries,
    _venv_interpreter_path,
    _venv_interpreter_path_discover,
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


class _NeverAnswersBus:
    """Real RequestBus-compatible Fake whose ``request`` coroutine never
    resolves (#3048): models the codeact/headless dispatch scenario where a
    bus IS wired (so ``require_http_get`` takes the ``await self._approve``
    branch, not the ``bus is None`` fast-fail) but nobody is listening to
    answer the intervention — the confirmed root cause of the indefinite
    await that the caller's compute-budget timeout then guillotines. This is
    a real, minimal implementation of the ``RequestBus`` protocol (not a
    mock/patch): its ``request`` genuinely never completes, exactly like an
    unattended bus in production."""

    def __init__(self) -> None:
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        await asyncio.Event().wait()  # never set — never returns
        raise AssertionError("unreachable: the wait() above never resolves")


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
    config_permissions: dict | None = None,
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
    + ``bus`` drive the run-code trust prompt (which never persists).

    ``config_permissions`` (default None → ``{}``) is passed straight to the
    ``PermissionResolver`` — used by the #3048 config-deny witness test to
    prove config-tier ``deny`` still wins over the derived pypi.org grant."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions=config_permissions or {},
        project_root=project_root, interactive=interactive,
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
    requirements.txt — offline, no materialise step at all)."""
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
#
# #3202 symptom 2: materialise no longer depends on `uv` — `<sys.executable>
# -m venv` + `<venv_python> -m pip install` (both bundled with reyn's own
# CPython), so these tests are NOT gated on a `uv` binary being on PATH.


@pytest.mark.asyncio
async def test_materialise_deps_rewrites_mcp_spawn_to_venv_interpreter(tmp_path, monkeypatch):
    """Tier 2: §3.11 headline property — a plugin with a requirements.txt + an
    mcp capability (command: python) materialises a per-plugin venv at INSTALL
    time, and the registered mcp spawn command is rewritten to that venv's
    interpreter — so spawn needs no network (spawn execs the frozen absolute
    interpreter path directly; it never re-invokes pip).

    Uses an EMPTY requirements.txt so `<sys.executable> -m venv` + `<venv_python>
    -m pip install -r` run fully offline (no package fetch) yet still exercise
    the real materialise + command-rewrite path. RED if the registered command
    stays 'python' (spawn would then depend on ambient env / fetch)."""
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

    venv_python = _venv_interpreter_path(plugins_root() / "venvplugin" / ".venv")
    assert venv_python.exists(), "per-plugin venv interpreter was not materialised at install time"

    mcp_yaml = ctx.workspace.base_dir / ".reyn" / "config" / "mcp.yaml"
    servers = yaml.safe_load(mcp_yaml.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert servers["srv"]["command"] == str(venv_python), (
        "registered mcp spawn command was not rewritten to the venv interpreter "
        "(spawn would not be network-free)"
    )
    assert servers["srv"]["plugin_id"] == "venvplugin"


# ── Test 6b: interpreter resolution across venv layouts (#3202 symptom 1) ────
#
# `_venv_interpreter_path` resolves the interpreter via stdlib `sysconfig`'s
# "venv" install scheme (no hardcoded bin/Scripts branch, no subprocess).
# `_venv_interpreter_path_discover` is its on-disk-existence FALLBACK, used
# only if the sysconfig computation's result doesn't actually exist — these
# witnesses build the REAL file layout on disk (a Windows-shaped venv can be
# reproduced on any host by just creating the `Scripts/python.exe` file)
# instead of monkeypatching `os.name`/`sys.platform`.


def test_venv_interpreter_path_resolves_real_venv_via_sysconfig(tmp_path):
    """Tier 1: `_venv_interpreter_path` against a REAL `python -m venv`
    (this repo's own interpreter, no network) returns a path that actually
    exists and is executable — the sysconfig-computed primary mechanism,
    not the discovery fallback, since sysconfig has no reason to fail here.
    RED if sysconfig computes a nonexistent path for a real venv."""
    venv_dir = tmp_path / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True, capture_output=True,
    )

    resolved = _venv_interpreter_path(venv_dir)

    assert resolved.exists(), f"sysconfig-resolved interpreter does not exist: {resolved}"
    probe = subprocess.run(
        [str(resolved), "-c", "print('alive')"], capture_output=True, text=True,
    )
    assert probe.returncode == 0 and "alive" in probe.stdout


def test_venv_interpreter_path_discover_finds_posix_layout(tmp_path):
    """Tier 1: given a venv dir with ONLY the POSIX layout
    (`<venv>/bin/python`) present on disk, the fallback discovery helper
    returns that file. RED if it returns a Windows-shaped path or raises."""
    venv_dir = tmp_path / ".venv"
    posix_python = venv_dir / "bin" / "python"
    posix_python.parent.mkdir(parents=True)
    posix_python.write_bytes(b"")

    assert _venv_interpreter_path_discover(venv_dir) == posix_python


def test_venv_interpreter_path_discover_finds_windows_layout(tmp_path):
    """Tier 1: given a venv dir with ONLY the Windows layout
    (`<venv>/Scripts/python.exe`) present on disk — reproducing a real
    Windows venv without needing a Windows host — the fallback discovery
    helper returns THAT file, not the POSIX `bin/python` construction that
    caused #3202 symptom 1."""
    venv_dir = tmp_path / ".venv"
    windows_python = venv_dir / "Scripts" / "python.exe"
    windows_python.parent.mkdir(parents=True)
    windows_python.write_bytes(b"")

    assert _venv_interpreter_path_discover(venv_dir) == windows_python


def test_venv_interpreter_path_discover_raises_when_neither_layout_exists(tmp_path):
    """Tier 1: an empty/nonexistent venv dir (neither layout materialised —
    evidence venv creation did not actually produce an interpreter) raises
    `FileNotFoundError` explicitly rather than silently returning a
    nonexistent path for the caller to fail on later without context."""
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        _venv_interpreter_path_discover(venv_dir)


def test_venv_interpreter_path_falls_back_to_discovery_when_sysconfig_path_absent(
    tmp_path,
):
    """Tier 1: axis-4-adjacent — if the sysconfig-computed path does NOT
    exist on disk (the pathological case the fallback exists for), and only
    a DIFFERENT layout's file is actually present, `_venv_interpreter_path`
    still returns a real, existing interpreter via the discovery fallback
    rather than the (nonexistent) sysconfig-computed one. Reproduces the
    fallback path deterministically by placing ONLY the Windows layout on
    disk — sysconfig on this (non-Windows) test runner computes the POSIX
    path, which will not exist, forcing the fallback branch."""
    venv_dir = tmp_path / ".venv"
    windows_python = venv_dir / "Scripts" / "python.exe"
    windows_python.parent.mkdir(parents=True)
    windows_python.write_bytes(b"")

    resolved = _venv_interpreter_path(venv_dir)

    assert resolved == windows_python, (
        "expected the discovery fallback to find the Windows-layout file "
        "when sysconfig's own computed path does not exist"
    )


def test_build_mcp_entries_rewrites_to_windows_style_interpreter():
    """Tier 1: `_build_mcp_entries` rewrites a bare `python` command to
    WHATEVER `venv_python` path it is given — including a Windows-shaped
    (`Scripts/python.exe`) path — confirming the spawn side consumes the
    resolved path unchanged (the same resolver serves both install and
    spawn, per the single-helper aggregation)."""
    windows_python = Path("C:/plugins/srv/.venv/Scripts/python.exe")
    mcp_json = {
        "mcpServers": {"srv": {"command": "python", "args": ["-m", "srv"]}},
    }
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as fh:
        json.dump(mcp_json, fh)
        mcp_json_path = Path(fh.name)
    try:
        entries = _build_mcp_entries(mcp_json_path, windows_python)
    finally:
        mcp_json_path.unlink()

    assert entries["srv"]["command"] == str(windows_python)


# ── Test 6c: TMPDIR containment under a REAL enforcing sandbox backend ────────
# (#3202 symptom 2 pip-materialise). Honest finding (measured directly, not
# assumed): `_materialise_deps` already passes `cwd=str(plugin_root)` to every
# `backend.run()` call, and CPython's own `tempfile._get_default_tempdir()`
# candidate list ends with `os.getcwd()` as a last resort BEFORE raising — so
# with our actual call shape, a `tempfile.mkdtemp()`-based write (verified
# directly: a real `pip install six` under Seatbelt with `write_paths=
# [plugin_root]`, `cwd=plugin_root`, and NO TMPDIR override) already succeeds
# via that built-in cwd fallback, landing inside `plugin_root`. The TMPDIR
# redirect this fix adds is still kept as explicit, observable containment
# (a future refactor that drops the `cwd=` argument, or a pip/tempfile
# version that narrows its fallback chain, would silently lose the cwd
# safety net) — but it is NOT provable as "removing it flips this to RED" in
# the CURRENT call shape, and this test says so rather than asserting a
# denial that direct measurement showed does not occur here. What IS
# verified below: with TMPDIR explicitly set (mirroring `_materialise_deps`),
# the temp write lands inside the designated dir, not system `/tmp`.


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_tmpdir_redirect_contains_temp_write_under_plugin_root(tmp_path):
    """Tier 2: with `_materialise_deps`'s TMPDIR redirect applied (env var
    pointed at a dir inside `plugin_root`), a `tempfile.mkdtemp()`-based
    write under a REAL Seatbelt backend (`write_paths=[plugin_root]`) lands
    inside the designated `.pip-tmp` dir — confirming the redirect actually
    takes effect (the env value is honoured), not merely that SOME fallback
    happens to succeed."""
    import asyncio as _asyncio

    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec unavailable on this host")

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    tmp_dir = plugin_root / ".pip-tmp"
    tmp_dir.mkdir()
    policy_with_tmpdir = SandboxPolicy(
        network=False,
        write_paths=[str(plugin_root)],
        allow_subprocess=True,
        timeout_seconds=30,
        env_passthrough=["TMPDIR"],
    )
    probe_code = (
        "import tempfile, os; "
        "d = tempfile.mkdtemp(); "
        "open(os.path.join(d, 'probe'), 'w').write('x'); "
        "print(d)"
    )

    previous_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(tmp_dir)
    try:
        result = _asyncio.run(
            backend.run(
                [sys.executable, "-c", probe_code],
                policy_with_tmpdir, cwd=str(plugin_root),
            ),
        )
    finally:
        if previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmpdir

    assert result.returncode == 0, (
        f"expected the TMPDIR-redirected write to succeed: "
        f"stderr={result.stderr.decode('utf-8', errors='replace')}"
    )
    used_dir = result.stdout.decode("utf-8", errors="replace").strip()
    assert used_dir.startswith(str(tmp_dir)), (
        f"expected tempfile.mkdtemp() to land inside the redirected TMPDIR "
        f"({tmp_dir}), got {used_dir!r} — the env var redirect did not take effect"
    )


# ── Test 7: pypi dep-fetch gate (require_http_get on the package index) ───────
#
# #3048: the dep-fetch approval for pypi.org is now DERIVED from the
# install's own gate-1 write-approval (session_approve_host, scoped to
# exactly "pypi.org") instead of requiring an INDEPENDENT interactive
# http.get prompt. A plugin's install approval alone is therefore now
# sufficient for materialise-deps to proceed — the config-deny and
# sandbox-network-veto tiers (checked BEFORE the persisted-approval tier
# the derive feeds, inside require_http_get) are the only things that can
# still block it. The two tests below replace the pre-#3048
# "http.get independently deniable while write-approved" test (that
# behaviour was the confused-deputy-adjacent bug: a SEPARATE, unanswerable
# prompt that hung the whole install under codeact's 30s budget).


@pytest.mark.asyncio
async def test_dep_materialise_denied_when_config_denies_pypi_host(tmp_path, monkeypatch):
    """Tier 2: #3048 — the #3048 derive does NOT bypass a config-tier deny.
    ``http.get.pypi.org: deny`` still blocks dep-materialisation even though
    the install's gate-1 write is approved (plugins_root approved, so the
    copy proceeds and we reach the materialise step) — config-deny is
    checked BEFORE the persisted-approval tier the derive feeds inside
    ``require_http_get``, so it still wins. RED if the derive silently
    overrides an explicit operator/config denial."""
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

    ctx = _make_ctx(
        tmp_path, approve_plugins_root=True, approve_all_http=False, interactive=False,
        config_permissions={"http.get.pypi.org": "deny"},
    )

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)
    venv = plugins_root() / "needsdeps" / ".venv"
    assert not venv.exists(), "a venv/fetch happened despite config http.get.pypi.org: deny"


@pytest.mark.asyncio
async def test_derive_does_not_bypass_install_gate1_denial(tmp_path, monkeypatch):
    """Tier 2: #3048 — the derive is fed by gate 1 SUCCEEDING; if gate 1
    itself is denied (plugins_root NOT approved), execution never reaches
    the derive/materialise step at all, and no venv is created. RED if the
    derive were (incorrectly) hoisted above/independent of gate 1."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = tmp_path / "src" / "needsdeps3"
    (src / ".reyn-plugin").mkdir(parents=True)
    (src / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "needsdeps3", "version": "0.1.0", "capabilities": []}),
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text("somepkg==1.0\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, approve_plugins_root=False, approve_mcp_yaml=False, interactive=False)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    with pytest.raises(PermissionError):
        await install_handle(op, ctx)
    venv = plugins_root() / "needsdeps3" / ".venv"
    assert not venv.exists(), "materialise ran despite gate 1 (install write) being denied"


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
    assert _venv_interpreter_path(plugins_root() / "okdeps" / ".venv").exists()


@pytest.mark.asyncio
async def test_materialise_succeeds_with_uv_stripped_from_path(tmp_path, monkeypatch):
    """Tier 2: #3202 symptom 2 direct witness. With EVERY PATH entry that
    could resolve a `uv` binary removed (verified: `shutil.which("uv")`
    returns None under the stripped PATH), a REAL dep-materialise — a tiny
    real PyPI package (`six`, pure Python, no build step) via a real network
    fetch — still succeeds. `_materialise_deps` no longer names `uv`
    anywhere, so its absence must be a non-event; RED if materialise still
    somehow depended on it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    import shutil as _shutil
    real_path = os.environ.get("PATH", "")
    stripped_entries = [
        entry for entry in real_path.split(os.pathsep)
        if not (Path(entry) / "uv").exists() and not (Path(entry) / "uv.exe").exists()
    ]
    stripped_path = os.pathsep.join(stripped_entries)
    monkeypatch.setenv("PATH", stripped_path)
    assert _shutil.which("uv") is None, (
        "test setup bug: `uv` is still resolvable on the stripped PATH — "
        "this witness would not actually exercise a uv-less environment"
    )

    src = tmp_path / "src" / "nouvdeps"
    (src / ".reyn-plugin").mkdir(parents=True)
    (src / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "nouvdeps", "version": "0.1.0", "capabilities": []}),
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text("six\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, approve_plugins_root=True, approve_all_http=True)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", f"materialise failed without uv on PATH: {result}"
    venv_python = _venv_interpreter_path(plugins_root() / "nouvdeps" / ".venv")
    assert venv_python.exists()
    site_packages = subprocess.run(
        [str(venv_python), "-c", "import six; print(six.__file__)"],
        capture_output=True, text=True,
    )
    assert site_packages.returncode == 0 and "six" in site_packages.stdout, (
        f"real dependency (six) was not actually installed/importable: {site_packages}"
    )


# ── #3048 seal: require_http_get awaits indefinitely when a bus is wired ──────
# but nothing answers it (the confirmed root cause — NOT budget exhaustion,
# NOT a slow download: a permission prompt raised into a bus with no
# responder). Live-confirms the mechanism BEFORE trusting the fix below to
# actually close it.


@pytest.mark.asyncio
async def test_require_http_get_awaits_indefinitely_when_unanswered(tmp_path):
    """Tier 1: #3048 seal — require_http_get(host) with a bus PRESENT (not
    None, so the fast-fail ``bus is None`` branch is NOT taken) and the host
    NOT approved awaits the intervention response with no internal timeout.
    Real PermissionResolver + a real RequestBus implementation
    (``_NeverAnswersBus``) whose ``request`` coroutine genuinely never
    resolves (the codeact/headless scenario: a bus is wired but no
    responder is listening). Confirms the ④-a dogfood witness's structural
    diagnosis (30s codeact kill on a never-answered prompt, not a slow
    download) — the caller's own ``asyncio.wait_for`` bound is what
    terminates this test, not anything inside ``require_http_get`` itself.

    ``interactive=True`` mirrors the real dispatch (``sys.stdin.isatty()``
    at an interactive terminal — the chat session IS interactive; nobody
    just happens to answer this particular prompt, e.g. an
    auto-driving/dogfood loop). With ``interactive=False`` (a genuinely
    headless dispatch), ``_approve`` fast-denies instead — that path is
    NOT #3048's mechanism and is covered by the other gate tests."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=True)
    decl = PermissionDecl(http_get=[{"host": "pypi.org"}])
    bus = _NeverAnswersBus()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            resolver.require_http_get(decl, "pypi.org", bus, "test"), timeout=0.2,
        )
    assert bus.asks, "no intervention request was raised — require_http_get did not reach the prompt path"
    assert bus.asks[0].kind == "permission.generic", (
        f"unexpected intervention kind {bus.asks[0].kind!r}: not the http.get permission prompt"
    )


# ── #3048 fix: install-grant derives the pypi.org dep-fetch approval ──────────


@pytest.mark.asyncio
async def test_plugin_install_derives_pypi_grant_no_indefinite_await(tmp_path, monkeypatch):
    """Tier 2: #3048 fix, load-bearing. Only the install's gate-1 write is
    approved (mirrors what an operator/codeact dispatch that approved
    "install this plugin" actually consented to) — pypi.org is
    deliberately NOT independently pre-approved. A ``_NeverAnswersBus`` is
    wired (the codeact/headless scenario that hung indefinitely pre-fix,
    same mechanism the seal test above confirms). Bounded with
    ``asyncio.wait_for`` well under a materialise-only budget: GREEN
    (completes, no hang) with the derive in plugin_install.py's step 7;
    RED (times out — falls into the seal test's await) if that derive is
    removed and require_http_get falls back to prompting the
    never-answering bus directly. Also asserts the bus received ZERO
    intervention requests — the derive must suppress the prompt outright,
    not merely answer it faster.

    ``interactive=True`` — same rationale as the seal test above: it
    mirrors the real interactive-terminal dispatch that #3048 hung
    (``sys.stdin.isatty()`` True, nobody happens to answer this specific
    prompt), which is exactly the branch where a stripped derive would
    hang rather than fast-deny."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = tmp_path / "src" / "needsdeps4"
    (src / ".reyn-plugin").mkdir(parents=True)
    (src / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({"name": "needsdeps4", "version": "0.1.0", "capabilities": []}),
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text("", encoding="utf-8")

    bus = _NeverAnswersBus()
    ctx = _make_ctx(
        tmp_path, approve_plugins_root=True, approve_all_http=False,
        interactive=True, bus=bus,
    )

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    result = await asyncio.wait_for(install_handle(op, ctx), timeout=30.0)

    assert result["status"] == "installed", f"install failed/denied: {result}"
    assert _venv_interpreter_path(plugins_root() / "needsdeps4" / ".venv").exists()
    assert not bus.asks, (
        "a separate pypi.org intervention prompt was raised — the derive did "
        "not suppress it (this is exactly the prompt that hangs under "
        "codeact's 30s compute budget when nobody answers it)"
    )


# ── #3048 security witness: the derive is host-scoped, not blanket http.get ───


@pytest.mark.asyncio
async def test_derived_pypi_grant_is_host_scoped_not_blanket(tmp_path):
    """Tier 1: #3048 security witness — confused-deputy guard. The derive
    plugin_install.py performs (``session_approve_host("pypi.org", ...,
    kind="http.get")``) covers EXACTLY pypi.org — the fixed index
    ``pip install`` resolves against — never http.get generally.
    Approving pypi.org must NOT silently authorise a fetch from an
    unrelated host. Uses ONLY the public PermissionResolver surface
    (``session_approve_host`` / ``require_http_get``), real instances
    throughout, no private-state assertions."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    resolver.session_approve_host("pypi.org", "test", kind="http.get")

    decl = PermissionDecl(http_get=[{"host": "*"}])
    # pypi.org: covered by the derive — resolves with no bus/prompt at all.
    await resolver.require_http_get(decl, "pypi.org", None, "test")
    # evil.com: NOT covered by the derive — falls through to the interactive
    # prompt path, which fast-fails (bus=None) rather than silently passing.
    # This is the exact evidence the derive is not a blanket http.get grant.
    with pytest.raises(PermissionError, match="requires an interactive prompt"):
        await resolver.require_http_get(decl, "evil.com", None, "test")
