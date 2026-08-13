"""Tier 2: OS invariant — `/plugin install`/`uninstall` slash surface (ADR 0064 §3.9, P3).

Same contract as ``tests/interfaces/test_plugin_cli_surface.py`` for the slash surface:
``/plugin`` is a thin adapter over the SAME typed op — it builds a
``ToolContext`` from this session's LIVE ``RouterHostAdapter``
(``build_resource_caller_state(session.router_host)``, i.e. the SAME factory
a live LLM ``install_plugin``/``__uninstall`` tool call gets) and
calls ``invoke_tool(get_default_registry(), "install_plugin"/"__uninstall", ...)``.

Tests:
  1. usage-error paths (no args, unknown kind, unknown subcommand, malformed
     quoting) reply_error without touching the tool layer.
  2. the typed ``kind`` discriminator (``builtin``/``local``/``git`` slash arg)
     threads through to the correct ``{kind, ...}`` source shape — real
     dispatch, captured via a real (non-mock) capturing async function.
  3. ``as <INSTALL_NAME>`` threads to the op's ``name`` override.
  4. a real local-plugin install through the FULL live seam (real Session +
     real RouterHostAdapter + real op_runtime handler, no invoke_tool stub)
     reaches the same ``.reyn/config/skills.yaml`` / ``~/.reyn/plugins/``
     writes the CLI test / P2's own op-level tests assert on.
  5. error-status and PermissionError from the op layer surface as
     reply_error, not a crash.

No unittest.mock anywhere. Real ``Session`` (mirrors
``tests/runtime/test_2548_skill_hotreload_toggle_pr_b.py::_make_session``) + real
``RouterHostAdapter`` + real ``PermissionResolver`` throughout.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import plugin as plugin_slash
from reyn.plugins.manifest import PLUGIN_MANIFEST_SCHEMA_URL
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from reyn.security.permissions.permissions import PermissionResolver
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.slash import slash_ctx


def _ctx(session):
    """The context the production dispatch hands a slash handler; the
    transport records into this wrapper's own reply list."""
    return slash_ctx(session, recorder=session.outbox_calls)


# ── shared fixtures ─────────────────────────────────────────────────────────


class _ReplyCapturingSession:
    """A real Session, wrapped so tests can read the replies the handler
    displayed. #3595 S4 routes a slash reply through ``ClientTransport``, so
    the capture is the transport's recorder (``_ctx`` below hands it this
    list); the Session underneath is real, not faked."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self.outbox_calls: list[OutboxMessage] = []

    def __getattr__(self, item):
        return getattr(self._session, item)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self.outbox_calls if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self.outbox_calls if m.kind == "error")


def _make_session(
    tmp_path: Path, *, agent_name: str = "test-agent",
    permission_resolver: "PermissionResolver | None" = None,
    sandbox_config: "object | None" = None,
) -> _ReplyCapturingSession:
    """Minimal real Session in *tmp_path* (mirrors
    tests/runtime/test_2548_skill_hotreload_toggle_pr_b.py::_make_session), anchored
    at tmp_path so ``session.router_host.make_router_op_context()`` builds a
    real Workspace rooted there.

    ``sandbox_config``: the router-dispatched OpContext ALWAYS synthesizes a
    floor SandboxPolicy restricting ``write_paths`` to the workspace
    (``build_router_op_context`` / #1339 — closes a sandbox-escape gap; the
    SAME floor a live LLM ``install_plugin`` tool call gets, not
    a slash-specific restriction). A ``{kind:local}``/``{kind:git}`` install
    writes OUTSIDE the workspace (``~/.reyn/plugins/``), so exercising that
    path for real requires the operator-equivalent explicit
    ``sandbox.policy.write_paths`` grant, supplied here."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    session = make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        permission_resolver=permission_resolver,
        sandbox_config=sandbox_config,
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / ".reyn",
    )
    return _ReplyCapturingSession(session)


def _write_local_plugin(base: Path, name: str = "myplugin") -> Path:
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "$schema": PLUGIN_MANIFEST_SCHEMA_URL,
            "name": name, "version": "0.1.0", "description": "test plugin",
        }),
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "hello").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello\ndescription: says hi\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    return plugin_dir


# ── 1. usage errors (Tier 2: OS invariant — no crash on malformed input) ──


@pytest.mark.asyncio
async def test_no_args_is_usage_error(tmp_path) -> None:
    """Tier 2: `/plugin` with no arguments replies a usage error, not a crash."""
    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "")
    assert "usage" in session.error_text().lower()


@pytest.mark.asyncio
async def test_unknown_subcommand_is_usage_error(tmp_path) -> None:
    """Tier 2: an unrecognized subcommand (not install/uninstall) replies a
    usage error, not a crash."""
    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "frobnicate x")
    assert "unknown subcommand" in session.error_text().lower()


@pytest.mark.asyncio
async def test_unknown_kind_is_usage_error(tmp_path) -> None:
    """Tier 2: an unrecognized source kind (not builtin/local/git) replies a
    usage error — the typed discriminator rejects untyped/unknown values."""
    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "install registry rag")
    assert "unknown source kind" in session.error_text().lower()


@pytest.mark.asyncio
async def test_malformed_quoting_is_usage_error(tmp_path) -> None:
    """Tier 2: malformed shell-style quoting in the args string replies an
    error (ValueError from shlex.split caught), not a crash."""
    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), 'install local "unterminated')
    assert session.error_text(), "expected an error reply for malformed quoting"


# ── 2. typed kind discriminator threading (Tier 1: Contract) ──────────────


@pytest.mark.parametrize(
    "cmd_args,expected_source",
    [
        ("install builtin rag", {"kind": "builtin", "name": "rag"}),
        ("install local /tmp/some/dir", {"kind": "local", "path": "/tmp/some/dir"}),
        ("install git https://example.com/x.git", {"kind": "git", "url": "https://example.com/x.git"}),
    ],
)
@pytest.mark.asyncio
async def test_install_kind_threads_to_typed_source_shape(
    tmp_path, monkeypatch, cmd_args, expected_source,
) -> None:
    """Tier 1: each /plugin install subcommand (builtin/local/git) builds the
    EXACT typed {kind, ...} source shape — never a form-sniffed string."""
    captured: dict = {}

    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        captured["name"] = name
        captured["args"] = args
        return {"status": "ok", "data": {"status": "installed", "name": "x"}}

    monkeypatch.setattr(plugin_slash, "_invoke_plugin_tool", _fake_invoke)

    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), cmd_args)

    assert captured["name"] == "install_plugin"
    assert captured["args"]["source"] == expected_source
    assert "name" not in captured["args"]
    assert not session.error_text(), f"unexpected error: {session.error_text()}"


@pytest.mark.asyncio
async def test_install_as_name_overrides_thread_through(tmp_path, monkeypatch) -> None:
    """Tier 1: `as <NAME>` threads to the op's `name` override field."""
    captured: dict = {}

    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        captured["args"] = args
        return {"status": "ok", "data": {"status": "installed", "name": "custom"}}

    monkeypatch.setattr(plugin_slash, "_invoke_plugin_tool", _fake_invoke)

    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "install builtin rag as custom")

    assert captured["args"]["source"] == {"kind": "builtin", "name": "rag"}
    assert captured["args"]["name"] == "custom"


@pytest.mark.asyncio
async def test_uninstall_threads_name(tmp_path, monkeypatch) -> None:
    """Tier 1: `/plugin uninstall NAME` forwards {"name": NAME} to the
    uninstall_plugin op — no extra/renamed fields."""
    captured: dict = {}

    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        captured["name"] = name
        captured["args"] = args
        return {"status": "ok", "data": {"status": "uninstalled", "name": "myplugin"}}

    monkeypatch.setattr(plugin_slash, "_invoke_plugin_tool", _fake_invoke)

    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "uninstall myplugin")

    assert captured["name"] == "uninstall_plugin"
    assert captured["args"] == {"name": "myplugin"}


# ── 3. real install through the FULL live seam ─────────────────────────────


@pytest.mark.asyncio
async def test_slash_local_install_real_stack(tmp_path, monkeypatch) -> None:
    """Tier 2: `/plugin install local <path>` through the REAL Session →
    RouterHostAdapter → make_router_op_context → op_runtime handler stack
    (no invoke_tool stub) writes the SAME .reyn/config/skills.yaml +
    ~/.reyn/plugins/<name>/ copy the CLI test / P2's op-level tests assert on."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    project = tmp_path / "proj"
    project.mkdir()
    src = _write_local_plugin(tmp_path / "src")

    perm_resolver = PermissionResolver(
        config_permissions={"file.write": "allow"},
        project_root=project,
        interactive=False,
    )
    from reyn.config.infra import SandboxConfig
    # Explicit allow_write_paths REPLACE (not union with) the floor's
    # workspace-only default ("wrote it" semantics, #2964) — so both the
    # plugin global-copy root AND the project workspace must be listed for
    # the full install (copy + .reyn/config/skills.yaml registration) to
    # succeed.
    sandbox_config = SandboxConfig(policy={
        "allow_write_paths": [str(home / ".reyn" / "plugins"), str(project)],
    })
    session = _make_session(
        project, permission_resolver=perm_resolver, sandbox_config=sandbox_config,
    )

    await plugin_slash.plugin_cmd(_ctx(session), f"install local {src}")

    assert not session.error_text(), f"unexpected error: {session.error_text()}"
    plugin_copy = home / ".reyn" / "plugins" / "myplugin"
    assert plugin_copy.is_dir(), "plugin copy not written under ~/.reyn/plugins/"
    skills_yaml = project / ".reyn" / "config" / "skills.yaml"
    assert skills_yaml.exists(), "skills registry entry not written"
    registered = yaml.safe_load(skills_yaml.read_text(encoding="utf-8"))
    assert "hello" in (registered.get("skills") or {}).get("entries", {})

    await plugin_slash.plugin_cmd(_ctx(session), "uninstall myplugin")
    assert not session.error_text(), f"unexpected error on uninstall: {session.error_text()}"
    assert not plugin_copy.exists(), "plugin copy not removed on uninstall"


# ── 4. error surfacing (op-layer failures → reply_error, not a crash) ─────


@pytest.mark.asyncio
async def test_op_error_status_surfaces_as_reply_error(tmp_path, monkeypatch) -> None:
    """Tier 2: an op-layer {"status": "error"} data envelope (e.g. unknown
    builtin plugin) surfaces as a reply_error, not a silent success."""
    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        return {"status": "ok", "data": {"status": "error", "error": "boom"}}

    monkeypatch.setattr(plugin_slash, "_invoke_plugin_tool", _fake_invoke)

    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "install builtin rag")
    assert "boom" in session.error_text()


@pytest.mark.asyncio
async def test_permission_error_surfaces_as_reply_error(tmp_path, monkeypatch) -> None:
    """Tier 2: a PermissionError raised by the op layer (e.g. the {kind:git}
    run-code trust gate denying) surfaces as a reply_error, not a crash."""
    async def _fake_invoke(name: str, args: dict, ctx) -> dict:
        raise PermissionError("denied for testing")

    monkeypatch.setattr(plugin_slash, "_invoke_plugin_tool", _fake_invoke)

    session = _make_session(tmp_path)
    await plugin_slash.plugin_cmd(_ctx(session), "install git https://example.com/x.git")
    assert "denied for testing" in session.error_text()
