"""Tier 2: #5213 — a per-session ``disabled:`` entry may only disable a
hook whose origin is per-agent or per-session, never a startup or runtime
(project-level) hook.

Root cause (architect ruling, issue #5213): ``hooks:`` composition used to
concatenate every layer's raw dicts into ONE flat list before parsing —
provenance was discarded at concatenation, so ``Session._hook_dispatcher``'s
``is_hook_disabled`` predicate had no way to ask "which layer declared this
hook?". Any agent that can write its own per-session state (every agent —
``<state dir>/hooks.yaml``'s ``disabled:`` list is inside
``_DEFAULT_WRITE_ZONES``) could therefore silently neutralise a
project-level (startup or runtime) hook the operator declared specifically
because those two layers are the ones the agent cannot write (#5213's own
motivating example: #5041's supervision hook, placed at the runtime layer —
``.reyn/config/hooks.yaml`` — precisely because it is protected).

Fix: ``HookDef`` now carries ``origin`` (the config layer), and
``is_hook_disabled`` checks
``hook_origin_is_at_least_as_specific_as(hook.origin, "per-agent")`` in
addition to the name match — ``startup`` and ``runtime`` stay protected;
``per-agent`` and ``per-session`` remain disableable (both are inside every
agent's default write zone, so ``disabled:`` grants no new power there).
Real ``Session``/``AgentRegistry``/``HookDispatcher`` — no mocks, mirrors
``test_hook_applicability_2285.py``'s own real-seam pattern.

Threshold correction (#5218 review): an earlier version of the fix used
``"runtime"`` as the threshold, on the (incorrect) assumption that
``.reyn/hooks.yaml`` — a stale, pre-#2073-file-split filename — was the
runtime layer's actual file and was agent-writable. The runtime layer is
actually ``.reyn/config/hooks.yaml``, which sits under
``_RECOVERY_CORE_WRITE_PREFIXES`` and is NOT agent-writable — the
``"runtime"`` threshold left #5041's supervision hook one ``disabled:``
entry away from being switched off by the party it supervises. See
``test_a_runtime_layer_hook_remains_protected`` below for the corrected
acceptance witness.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_STARTUP_HOOK_NAME = "project-supervision-hook"
_STARTUP_HOOKS = [
    {
        "on": "turn_end",
        "name": _STARTUP_HOOK_NAME,
        "template_push": {"message": "startup fired", "wake": True},
    },
]


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return make_session(
        agent_name="alice",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / ".reyn" / "agents" / "alice" / "state" / "snapshot.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


@pytest.mark.asyncio
async def test_a_project_layer_hook_still_fires_after_a_session_layer_disable(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — the exact #5213 witness. A ``disabled:`` entry
    at the session layer naming a project(startup)-layer hook must NOT
    stop it from firing — the origin check rejects the name match because
    the hook's origin (``startup``) is LESS specific than the disabling
    layer (``per-session``)."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)

    # The agent writes ITS OWN per-session disabled-set naming the
    # project-layer hook — exactly what #5213 describes: any agent that
    # can write its own state can do this today, pre-fix.
    s.set_hook_enabled(_STARTUP_HOOK_NAME, False)

    await s._hook_dispatcher.dispatch("turn_end", {})

    assert s.inbox.qsize() >= 1, (
        "a project-layer hook must still fire even after the session "
        "layer's own disabled: list names it — the origin check must "
        "reject this cross-layer disable"
    )


@pytest.mark.asyncio
async def test_a_per_session_layer_hook_can_still_disable_itself(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast — the SAME disable mechanism must
    still work for a hook that genuinely originates at the per-session
    layer — #5213 narrows the SCOPE of ``disabled:`` (only ``startup`` and
    ``runtime`` are protected — see ``is_hook_disabled``'s own comment), it
    does not remove the feature."""
    import yaml

    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=None)
    per_session_hooks_path = Path(s._snapshot_path).parent / "hooks.yaml"
    per_session_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    per_session_hooks_path.write_text(
        yaml.safe_dump({
            "hooks": [
                {
                    "on": "turn_end",
                    "name": "session-own-hook",
                    "template_push": {"message": "session fired", "wake": True},
                },
            ],
        }),
        encoding="utf-8",
    )
    await s._reapply_hooks({})  # re-read all layers, including the new per-session file

    s.set_hook_enabled("session-own-hook", False)
    await s._hook_dispatcher.dispatch("turn_end", {})

    assert s.inbox.qsize() == 0, (
        "a genuinely per-session-origin hook must still be disableable by "
        "its own layer's disabled: list"
    )


@pytest.mark.asyncio
async def test_a_per_agent_layer_hook_remains_disableable(tmp_path, monkeypatch) -> None:
    """Tier 2: falsification contrast — the write-zone boundary
    (``per-agent``/``per-session`` are BOTH in ``_DEFAULT_WRITE_ZONES``,
    confirmed via ``_canonical_protected_write_paths()``: the agent could
    edit ``.reyn/agents/<name>/hooks.yaml`` directly to remove this hook
    anyway, so ``disabled:`` grants no new power here). Mirrors
    ``test_hook_applicability_2285.py``'s own pre-existing per-agent-hook
    disable tests — this is the exact real-world case #5213's fix must NOT
    break (found by running the full suite, not guessed)."""
    import yaml

    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=None)
    per_agent_hooks_path = tmp_path / ".reyn" / "agents" / "alice" / "hooks.yaml"
    per_agent_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    per_agent_hooks_path.write_text(
        yaml.safe_dump({
            "hooks": [
                {
                    "on": "turn_end",
                    "name": "agent-own-hook",
                    "template_push": {"message": "agent fired", "wake": True},
                },
            ],
        }),
        encoding="utf-8",
    )
    await s._reapply_hooks({})

    s.set_hook_enabled("agent-own-hook", False)
    await s._hook_dispatcher.dispatch("turn_end", {})

    assert s.inbox.qsize() == 0, (
        "a per-agent-origin hook must remain disableable — the agent "
        "already has write access to the file that declares it"
    )


@pytest.mark.asyncio
async def test_a_runtime_layer_hook_remains_protected(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — the CORRECTED write-zone boundary (architect
    finding, #5218 review). The ``runtime`` layer (the IN-set's ``hooks:``
    key, physically ``.reyn/config/hooks.yaml``) sits under
    ``_RECOVERY_CORE_WRITE_PREFIXES`` (``.reyn/config/``, ``.reyn/state/``),
    which ``_in_default_write_zone`` excludes from the broad ``.reyn/``
    grant — a raw ``file.write`` there is denied, so it is NOT agent-
    writable the way ``per-agent``/``per-session`` are. A per-session
    ``disabled:`` entry naming a runtime-origin hook must therefore NOT
    stop it from firing, same as the startup case — this is the exact
    #5041 threat an earlier ("runtime") threshold left open (a runtime-
    origin supervision hook one ``disabled:`` line away from being
    switched off by the party it supervises). Constructs the runtime
    layer directly via ``in_set={"hooks": [...]}`` (mirrors
    ``Session._build_hook_registry``'s own ``(in_set or {}).get("hooks")``
    read, session.py:5679) rather than writing
    ``.reyn/config/hooks.yaml`` + exercising the full hot-reload file-read
    path — the origin-check boundary being tested here is downstream of
    that read, not the read itself."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=None)
    await s._reapply_hooks({
        "hooks": [
            {
                "on": "turn_end",
                "name": "runtime-own-hook",
                "template_push": {"message": "runtime fired", "wake": True},
            },
        ],
    })

    s.set_hook_enabled("runtime-own-hook", False)
    await s._hook_dispatcher.dispatch("turn_end", {})

    assert s.inbox.qsize() >= 1, (
        "a runtime-origin hook must still fire even after the session "
        "layer's own disabled: list names it — runtime is under "
        "_RECOVERY_CORE_WRITE_PREFIXES, not agent-writable"
    )


@pytest.mark.asyncio
async def test_strip_the_origin_check_reproduces_the_bypass(tmp_path, monkeypatch) -> None:
    """Tier 2: strip-falsify (architect's own witness prescription) — with
    the origin check removed from the predicate (mirroring the pre-#5213
    shape: name match alone), the project-layer hook IS disabled by the
    session-layer entry. Manually reconstructs the OLD predicate against
    the SAME real dispatcher/registry, rather than monkeypatching internal
    session state, so this test exercises the real HookDispatcher.dispatch
    path exactly like the fixed-behavior test above — only the predicate
    passed to it differs.

    #5230: ``set_hook_enabled`` itself now REFUSES to add a protected
    hook's name to ``_disabled_hooks`` at all (a further hardening on the
    write side, not just the read/dispatch side this test's predicate
    swap targets) — so seeding ``_disabled_hooks`` directly here (bypassing
    ``set_hook_enabled``) is what isolates the ORIGIN-CHECK regression this
    test exists to catch from the SEPARATE #5230 write-refusal behavior."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)
    s._disabled_hooks.add(_STARTUP_HOOK_NAME)

    old_predicate = lambda hook: hook.name is not None and hook.name in s._disabled_hooks  # noqa: E731
    s._hook_dispatcher._is_hook_disabled = old_predicate

    await s._hook_dispatcher.dispatch("turn_end", {})

    assert s.inbox.qsize() == 0, (
        "the OLD (origin-blind) predicate must reproduce the bypass — if "
        "this assertion fails, the strip did not actually revert to the "
        "pre-#5213 shape"
    )
