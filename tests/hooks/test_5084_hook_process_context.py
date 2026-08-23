"""Tier 1/2: #5084 ④ — a hook's ``exec``/``exec_capture`` child process
receives the CLOSED, 3-field ``REYN_*`` envelope (``HookProcessContext``,
mechanism "B" — see :mod:`reyn.runtime.workspace_paths`'s own module
docstring for the A/B split) via its own environment, and a relative argv
resolves inside the DISPATCHING AGENT'S OWN tree via ``cwd`` — both sourced
LIVE from ``HookDispatcher``'s injected callables (``hook_cwd``/
``hook_process_context``), never frozen at construction (#5081's
``_workspace_base_dir`` can change across the dispatcher's lifetime).

Before this: ``hooks/dispatcher.py``'s two ``_run_shell`` call sites passed
NO ``cwd`` at all — every hook exec silently inherited reyn's own launch
cwd, regardless of which agent dispatched it. This module witnesses the fix.

Real ``load_hooks``/``HookDispatcher``/``run_shell_hook`` throughout, plus a
real (non-mock) ``NoopBackend`` for the filesystem witness (③) — no mocks.
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import load_hooks
from reyn.hooks.shell_runner import HookProcessContext
from reyn.security.sandbox.backend import SandboxResult
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.sandbox_backend import FULLY_ENFORCING_AXES

# ── ② closed by construction (Tier 1) ────────────────────────────────────────


def test_hook_process_context_is_closed_to_exactly_three_named_fields():
    """Tier 1: ``HookProcessContext`` is a CLOSED envelope — a caller cannot
    add a fourth variable, and ``as_env()`` emits exactly these 3 keys, never
    more. This is what makes the type a typed contract (Tier-1 lens 2)
    instead of a free-form ``dict[str, str]``: the field/key SET is fixed by
    the class definition itself, not by whatever a caller happens to pass."""
    field_names = {f.name for f in fields(HookProcessContext)}
    assert field_names == {"project_dir", "agent_base_dir", "agent_name"}, (
        f"a 4th field would silently widen the envelope; got {field_names!r}"
    )

    ctx = HookProcessContext(
        project_dir=Path("/proj"), agent_base_dir=Path("/proj/agents/coder1"),
        agent_name="coder1",
    )
    assert set(ctx.as_env().keys()) == {
        "REYN_PROJECT_DIR", "REYN_AGENT_BASE_DIR", "REYN_AGENT_NAME",
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
