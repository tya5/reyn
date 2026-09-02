"""Tier 1/2: #5084 ④ — a hook's ``exec``/``exec_capture`` child process
receives the CLOSED, 4-field ``REYN_*`` envelope (``HookProcessContext``,
mechanism "B" — see :mod:`reyn.runtime.workspace_paths`'s own module
docstring for the A/B split) via its own environment, and a relative argv
resolves inside the DISPATCHING AGENT'S OWN tree via ``cwd`` — both sourced
LIVE from ``HookDispatcher``'s injected callables (``hook_cwd``/
``hook_process_context``), never frozen at construction (#5081's
``_workspace_base_dir`` can change across the dispatcher's lifetime).

#5208 added the 4th field, ``agent_state_dir``/``REYN_AGENT_STATE_DIR``: a
write target every agent can structurally reach regardless of ``base_dir``
narrowing (``.reyn`` sits inside ``_DEFAULT_WRITE_ZONES`` —
``reyn.security.permissions.permissions``). This module's own witnesses
below cover it: ①/② updated for 4 fields, plus a new strip-witness and a
base_dir-narrowed acceptance test (§ "four" section at the bottom).

Before this: ``hooks/dispatcher.py``'s two ``_run_shell`` call sites passed
NO ``cwd`` at all — every hook exec silently inherited reyn's own launch
cwd, regardless of which agent dispatched it. This module witnesses the fix.

Real ``load_hooks``/``HookDispatcher``/``run_shell_hook`` throughout, plus a
real (non-mock) ``NoopBackend`` for the filesystem witness (③) — no mocks.
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import fields
from pathlib import Path

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import load_hooks
from reyn.hooks.schema import hook_origin_is_at_least_as_specific_as
from reyn.hooks.shell_runner import HookProcessContext
from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.sandbox_backend import FULLY_ENFORCING_AXES

# ── ② closed by construction (Tier 1) ────────────────────────────────────────


def test_hook_process_context_is_closed_to_exactly_four_named_fields():
    """Tier 1: ``HookProcessContext`` is a CLOSED envelope — a caller cannot
    add a fifth variable, and ``as_env()`` emits exactly these 4 keys, never
    more. This is what makes the type a typed contract (Tier-1 lens 2)
    instead of a free-form ``dict[str, str]``: the field/key SET is fixed by
    the class definition itself, not by whatever a caller happens to pass."""
    field_names = {f.name for f in fields(HookProcessContext)}
    assert field_names == {
        "project_dir", "agent_base_dir", "agent_name", "agent_state_dir",
    }, (
        f"a 5th field would silently widen the envelope; got {field_names!r}"
    )

    ctx = HookProcessContext(
        project_dir=Path("/proj"), agent_base_dir=Path("/proj/agents/coder1"),
        agent_name="coder1", agent_state_dir=Path("/proj/.reyn/agents/coder1/state"),
    )
    assert set(ctx.as_env().keys()) == {
        "REYN_PROJECT_DIR", "REYN_AGENT_BASE_DIR", "REYN_AGENT_NAME",
        "REYN_AGENT_STATE_DIR",
    }


class _RecordingBackend:
    """Real (non-mock) SandboxBackend recording the ``hook_process_context``
    and ``cwd`` each dispatch was handed — same ``_RecordingBackend`` pattern
    as ``tests/hooks/test_hook_subprocess_per_site_2827.py``, extended to
    capture the 2 new #5084 ④ kwargs this module's witnesses need."""

    name = "recording"
    enforced_axes = FULLY_ENFORCING_AXES

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], "str | None", "HookProcessContext | None"]] = []

    def available(self) -> bool:
        return True

    async def run(
        self, argv, policy, *, stdin=None, cwd=None, cancel_event=None,
        hook_process_context=None,
    ):
        self.calls.append((list(argv), cwd, hook_process_context))
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")


async def _dispatch_with_agent(agent_name: str, backend: "_RecordingBackend") -> None:
    """Drive the REAL HookDispatcher with THIS agent's own
    hook_cwd/hook_process_context callables — mirrors the exact
    construction ``runtime/session.py`` performs at its own HookDispatcher
    call site (live callables, not frozen values)."""
    reg = load_hooks([{"on": "turn_end", "exec": ["echo", agent_name]}])
    dispatcher = HookDispatcher(
        reg,
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=backend,
        hook_cwd=lambda: f"/workspace/agents/{agent_name}",
        hook_process_context=lambda: HookProcessContext(
            project_dir=Path("/workspace"),
            agent_base_dir=Path(f"/workspace/agents/{agent_name}"),
            agent_name=agent_name,
            agent_state_dir=Path(f"/workspace/.reyn/agents/{agent_name}/state"),
        ),
    )
    await dispatcher.dispatch("turn_end", {})


# ── ① child reads REYN_AGENT_NAME, differs per agent (Tier 2) ───────────────


@pytest.mark.asyncio
async def test_dispatcher_threads_a_different_agent_name_per_agent(monkeypatch):
    """Tier 2: two agents' own dispatchers hand the sandbox backend TWO
    DIFFERENT ``HookProcessContext``s — ``REYN_AGENT_NAME`` in the resulting
    env is agent-specific, not a single process-wide value. A single-agent
    test cannot witness this: it would pass identically if hook_process_context
    were a frozen, shared value."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()

    await _dispatch_with_agent("coder1", backend)
    await _dispatch_with_agent("coder2", backend)

    names = {argv[-1]: ctx.as_env()["REYN_AGENT_NAME"] for argv, _cwd, ctx in backend.calls}
    assert names == {"coder1": "coder1", "coder2": "coder2"}


@pytest.mark.asyncio
async def test_project_origin_uses_project_cwd_while_agent_origin_uses_agent_cwd(monkeypatch):
    """Tier 2: project-layer hooks use the project tree; agent-layer hooks use the agent tree."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()
    dispatcher = HookDispatcher(
        load_hooks([{"on": "turn_end", "exec": ["echo", "project"]}], origin="runtime"),
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=backend,
        hook_cwd=lambda: "/workspace/agents/coder1",
        hook_cwd_for_origin=lambda origin: (
            "/workspace" if not hook_origin_is_at_least_as_specific_as(origin, "per-agent")
            else "/workspace/agents/coder1"
        ),
    )
    await dispatcher.dispatch("turn_end", {})
    [(_argv, cwd, _ctx)] = backend.calls
    assert cwd == "/workspace"


@pytest.mark.asyncio
async def test_real_sessions_run_project_and_agent_hook_in_distinct_trees(
    tmp_path, monkeypatch, out_of_process_reyn,
):
    """Tier 2: real sessions execute project and per-agent hooks in their declared trees.

    The child only runs a cwd probe and does not import Reyn; its script path
    is absolute, so the subprocess pin fixture is not needed here.
    """
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    monkeypatch.chdir(tmp_path)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    agent_tree = project / "repos" / "coder1"
    agent_tree.mkdir(parents=True)
    from reyn.runtime.session_params import ReactivityConfig
    from tests._support.agent_session import make_session

    runtime_script = project / "record_runtime_cwd.py"
    runtime_script.write_text(
        "from pathlib import Path\n"
        "Path('runtime_cwd.txt').write_text(str(Path.cwd()))\n",
        encoding="utf-8",
    )
    agent_script = project / "record_agent_cwd.py"
    agent_script.write_text(
        "from pathlib import Path\n"
        "Path('agent_cwd.txt').write_text(str(Path.cwd()))\n",
        encoding="utf-8",
    )
    runtime_hooks = project / ".reyn" / "config" / "hooks.yaml"
    runtime_hooks.parent.mkdir(parents=True, exist_ok=True)
    runtime_hooks.write_text(
        "hooks:\n  - on: session_start\n    write_paths: ["
        + repr(str(project))
        + "]\n    exec: ["
        + repr(sys.executable)
        + ", " + repr(str(runtime_script)) + "]\n",
        encoding="utf-8",
    )
    project_session = make_session(
        agent_name="project-agent", workspace_state_dir=project / ".reyn",
        workspace_base_dir=project, snapshot_path=project / ".reyn" / "project.json",
        state_log=None, reactivity=ReactivityConfig(hooks_config=[]),
    )
    await project_session.inbox.put(("shutdown", {}))
    await project_session.run()
    assert (project / "runtime_cwd.txt").read_text(encoding="utf-8") == str(project)
    (project / "runtime_cwd.txt").unlink()

    agent_hooks = project / ".reyn" / "agents" / "coder1" / "hooks.yaml"
    agent_hooks.parent.mkdir(parents=True, exist_ok=True)
    agent_hooks.write_text(
        "hooks:\n  - on: session_start\n    exec: ["
        + repr(sys.executable)
        + ", " + repr(str(agent_script)) + "]\n",
        encoding="utf-8",
    )
    agent_session = make_session(
        agent_name="coder1", workspace_state_dir=project / ".reyn",
        workspace_base_dir=agent_tree, snapshot_path=project / ".reyn" / "coder1.json",
        state_log=None, reactivity=ReactivityConfig(hooks_config=[]),
    )
    await agent_session.inbox.put(("shutdown", {}))
    await agent_session.run()
    assert (project / "runtime_cwd.txt").read_text(encoding="utf-8") == str(project)
    assert (agent_tree / "agent_cwd.txt").read_text(encoding="utf-8") == str(agent_tree)
    assert not (agent_tree / "runtime_cwd.txt").exists()


@pytest.mark.asyncio
async def test_per_agent_origin_uses_agent_cwd(monkeypatch):
    """Tier 2: a per-agent hook is handed the agent's own cwd."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()
    dispatcher = HookDispatcher(
        load_hooks([{"on": "turn_end", "exec": ["echo", "agent"]}], origin="per-agent"),
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=backend,
        hook_cwd=lambda: "/workspace/agents/coder1",
        hook_cwd_for_origin=lambda origin: (
            "/workspace" if not hook_origin_is_at_least_as_specific_as(origin, "per-agent")
            else "/workspace/agents/coder1"
        ),
    )
    await dispatcher.dispatch("turn_end", {})
    [(_argv, cwd, _ctx)] = backend.calls
    assert cwd == "/workspace/agents/coder1"


@pytest.mark.asyncio
async def test_shell_audit_event_records_origin_and_cwd(tmp_path, monkeypatch):
    """Tier 2: shell-hook audit events identify the declaring layer and cwd."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    events: list[tuple[str, dict]] = []
    dispatcher = HookDispatcher(
        load_hooks([{"on": "turn_end", "exec": ["true"]}], origin="runtime"),
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=NoopBackend(),
        hook_cwd_for_origin=lambda origin: "/workspace" if origin == "runtime" else "/agent",
        hook_temp_dir=lambda: str(tmp_path),
        emit_event=lambda event_type, **data: events.append((event_type, data)),
    )
    await dispatcher.dispatch("turn_end", {})
    shell_events = [data for event_type, data in events if event_type == "hook_shell_executed"]
    assert shell_events
    assert shell_events[-1]["cwd"] == "/workspace"
    assert shell_events[-1]["origin"] == "runtime"


@pytest.mark.asyncio
async def test_failed_shell_audit_event_records_origin_and_cwd(tmp_path, monkeypatch):
    """Tier 2: failed shell hooks retain cwd and origin in their audit event."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    events: list[tuple[str, dict]] = []
    dispatcher = HookDispatcher(
        load_hooks([{"on": "turn_end", "exec": [sys.executable, "-c", "raise SystemExit(3)"]}], origin="runtime"),
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=NoopBackend(),
        hook_cwd_for_origin=lambda origin: "/workspace" if origin == "runtime" else "/agent",
        hook_temp_dir=lambda: str(tmp_path),
        emit_event=lambda event_type, **data: events.append((event_type, data)),
    )
    await dispatcher.dispatch("turn_end", {})
    shell_events = [data for event_type, data in events if event_type == "hook_shell_executed"]
    assert shell_events
    assert shell_events[-1]["returncode"] == -1
    assert shell_events[-1]["cwd"] == "/workspace"
    assert shell_events[-1]["origin"] == "runtime"


@pytest.mark.asyncio
async def test_no_injected_callables_means_no_env_addition(monkeypatch):
    """Tier 2: regression guard — a dispatcher built WITHOUT hook_cwd/
    hook_process_context (every pre-#5084 construction site, including every
    pre-#5084 test) passes ``cwd=None, hook_process_context=None`` straight
    through, byte-identical to before these parameters existed."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    backend = _RecordingBackend()
    reg = load_hooks([{"on": "turn_end", "exec": ["echo", "hi"]}])
    dispatcher = HookDispatcher(
        reg,
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=backend,
    )
    await dispatcher.dispatch("turn_end", {})

    [(_argv, cwd, ctx)] = backend.calls
    assert cwd is None
    assert ctx is None


# ── ③ relative argv resolves inside the agent's OWN tree via cwd (Tier 2) ───


@pytest.mark.asyncio
async def test_relative_exec_argv_runs_inside_the_dispatching_agents_own_tree(tmp_path, monkeypatch):
    """Tier 2: strip-falsifier for the cwd-wiring half of #5084 ④ — a hook's
    RELATIVE exec argv writes into the agent's OWN base_dir, not reyn's own
    launch directory, driven through the REAL ``NoopBackend`` (no fake
    backend here — this is the actual host-process code path).

    Strip-falsifier: removing ``hook_cwd=``/the dispatcher.py cwd= wiring
    (reverting to the pre-#5084 ``_run_shell`` calls with no ``cwd``) makes
    the marker land in the process's OWN cwd instead of ``agent_dir`` —
    turns this red. Verified locally by temporarily dropping the two
    ``cwd=self._hook_cwd() ...`` lines in ``dispatcher.py``."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    agent_dir = tmp_path / "agents" / "coder1"
    agent_dir.mkdir(parents=True)
    other_dir = tmp_path / "agents" / "coder2"
    other_dir.mkdir(parents=True)

    reg = load_hooks(
        [{"on": "turn_end", "exec": ["touch", "marker.txt"]}]
    )
    dispatcher = HookDispatcher(
        reg,
        put_inbox=lambda *a, **k: None,
        stage_next_turn_context=lambda *a, **k: None,
        sandbox_backend=NoopBackend(),
        hook_cwd=lambda: str(agent_dir),
        hook_temp_dir=lambda: str(tmp_path),
    )
    await dispatcher.dispatch("turn_end", {})

    assert (agent_dir / "marker.txt").exists(), (
        "relative exec argv must resolve inside the dispatching agent's own "
        "base_dir, not the process's launch cwd"
    )
    assert not (other_dir / "marker.txt").exists(), (
        "a sibling agent's tree must be untouched — this is a PER-AGENT cwd, "
        "not a process-wide default"
    )


# ── ④ #5208: agent_state_dir reaches every agent regardless of base_dir ─────


@pytest.mark.asyncio
async def test_agent_state_dir_stays_under_the_project_reyn_dir_when_base_dir_is_narrowed(
    tmp_path,
):
    """Tier 2: strip-witness + acceptance for #5208.

    Six-questions ④: this MUST be taken with ``base_dir`` moved AWAY from
    the ``.reyn``-owning project root — a DEFAULT agent (base_dir ==
    project root) would pass trivially even if ``agent_state_dir`` were
    accidentally derived FROM ``base_dir`` instead of from the project's
    own ``.reyn/agents/<name>/state``, since the two paths would coincide.
    This test narrows ``workspace_base_dir`` to a SIBLING directory outside
    ``.reyn`` and asserts ``REYN_AGENT_STATE_DIR`` still resolves under the
    project's ``.reyn/agents/<name>/state`` — never under the narrowed
    ``base_dir`` — which is the whole point (a structurally write-reachable
    target for EVERY agent, `.reyn` is inside `_DEFAULT_WRITE_ZONES`,
    independent of how narrow `base_dir` gets).

    Strip-witness: removing ``REYN_AGENT_STATE_DIR`` from ``as_env()`` (or
    the ``agent_state_dir=`` kwarg at the real ``session.py`` construction
    site) makes ``ctx.as_env()["REYN_AGENT_STATE_DIR"]`` raise ``KeyError``
    here — verified locally by temporarily reverting the ``as_env()`` dict
    literal to its pre-#5208 3-key form.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    narrow_base = tmp_path / "elsewhere" / "narrow-base"
    narrow_base.mkdir(parents=True)

    from tests._support.agent_session import make_session

    session = make_session(
        agent_name="coder1",
        workspace_state_dir=project_root / ".reyn",
        workspace_base_dir=narrow_base,
    )

    expected_state_dir = (project_root / ".reyn" / "agents" / "coder1" / "state").resolve()
    assert not expected_state_dir.exists(), (
        "test setup sanity: nothing in this session's construction should "
        "have created the state dir yet — the point below is that reyn "
        "creates it, not that it happened to already be there"
    )

    # This is the live callable a real hook dispatch calls right before
    # exec/exec_capture — it must create the directory itself (reyn's own
    # responsibility, never a hook's child process's `mkdir`) so it exists
    # by the time ANY hook that reads REYN_AGENT_STATE_DIR actually runs.
    ctx = session._hook_dispatcher._hook_process_context()
    assert expected_state_dir.exists(), (
        "REYN_AGENT_STATE_DIR's target must exist once hook_process_context() "
        "has been called — reyn creates it, a hook is never asked to `mkdir` "
        "its own env var target"
    )
    assert ctx is not None
    resolved = Path(ctx.as_env()["REYN_AGENT_STATE_DIR"])
    assert resolved == expected_state_dir, (
        f"expected REYN_AGENT_STATE_DIR to stay under the project's own "
        f".reyn/agents/coder1/state ({expected_state_dir}) regardless of "
        f"the narrowed base_dir ({narrow_base}); got {resolved}"
    )
    assert narrow_base not in resolved.parents and resolved != narrow_base, (
        "agent_state_dir must never collapse into the narrowed base_dir — "
        "that would make it just as narrow as base_dir, defeating #5208's "
        "own point"
    )
