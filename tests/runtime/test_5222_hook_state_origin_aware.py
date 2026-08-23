"""Tier 2: #5222 — ``Session.hook_state()`` (the status-bar's hook read model)
reports ``enabled`` using the SAME predicate the real ``HookDispatcher``
enforces, not bare name-membership in ``self._disabled_hooks``.

Root cause (follow-up filed from #5218's own "Not touched" disclosure):
before this fix, ``hook_state()`` re-derived hook ``scope`` from a SEPARATE
raw-dict scan of the 4 config layers (re-reading ``.reyn/config/hooks.yaml``
+ the per-agent/per-session files a SECOND time, purely for display) and
computed ``enabled`` as bare ``name not in self._disabled_hooks`` — both
duplicating logic #5213 already made unnecessary (every ``HookDef`` in the
merged registry carries its own ``origin`` directly) and, worse,
INCONSISTENT with what #5213 actually enforces: a ``startup``- or
``runtime``-origin hook a session tried to disable via ``disabled:`` is
PROTECTED (#5213) and still fires, but pre-#5222 ``hook_state()`` reported
``enabled: false`` for it anyway — misleading exactly where it matters most
(#5041's own supervision hook: status bar reads as neutralized when it is
not).

Fix: ``hook_state()`` now reads ``HookDef.origin`` directly off
``self._hook_dispatcher.registry.all_defs()`` (the public accessors, never
the private ``registry._defs`` reach-in the old code used) and computes
``enabled`` via the SAME ``hook_origin_is_at_least_as_specific_as`` check
``is_hook_disabled`` uses. ``all_defs()`` is ordered least-to-most-specific
(startup → runtime → per-agent → per-session, see
``Session._build_hook_registry``); overwriting a per-name dict entry while
iterating forward reproduces "most-specific-layer-wins" for both ``scope``
and ``enabled`` from ONE walk, fixing a subtle inconsistency the old code
had: it computed ``scope`` as most-specific (via a similar forward-overwrite
loop) but picked its OTHER fields (via a separate ``seen`` set keeping the
FIRST-encountered ``HookDef``) from the LEAST-specific instance instead.

Real ``Session``/``HookDispatcher`` — no mocks, mirrors
``test_5213_hook_disable_layer_bypass.py``'s own real-seam pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
async def test_a_protected_startup_hook_disabled_via_per_session_shows_enabled_true(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — the exact #5222 witness. A per-session
    ``disabled:`` entry naming a startup-origin hook does not actually
    disable it (#5213) — ``hook_state()`` must say so (``enabled: True``),
    not the misleading ``enabled: False`` bare name-membership gave."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)

    s.set_hook_enabled(_STARTUP_HOOK_NAME, False)

    state = {h["name"]: h for h in s.hook_state()}
    assert state[_STARTUP_HOOK_NAME]["scope"] == "startup"
    assert state[_STARTUP_HOOK_NAME]["enabled"] is True, (
        "a startup-origin hook is protected from a per-session disable "
        "(#5213) — hook_state() must not report it as disabled"
    )

    # Corroborate against the REAL dispatch outcome, not just the display.
    await s._hook_dispatcher.dispatch("turn_end", {})
    assert s.inbox.qsize() >= 1, "the hook actually still fires — display must agree"


@pytest.mark.asyncio
async def test_a_genuine_per_session_hook_disable_shows_enabled_false(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast — a hook that genuinely originates at
    the per-session layer IS disableable (#5213 narrows scope, does not
    remove the feature), and ``hook_state()`` must still say so."""
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
    await s._reapply_hooks({})

    s.set_hook_enabled("session-own-hook", False)

    state = {h["name"]: h for h in s.hook_state()}
    assert state["session-own-hook"]["scope"] == "per-session"
    assert state["session-own-hook"]["enabled"] is False, (
        "a genuinely per-session-origin hook is disableable — hook_state() "
        "must reflect that"
    )

    await s._hook_dispatcher.dispatch("turn_end", {})
    assert s.inbox.qsize() == 0, "the hook is actually suppressed — display must agree"


@pytest.mark.asyncio
async def test_scope_reports_the_most_specific_layer_when_a_name_repeats(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: precedence — the SAME name declared at both the startup
    layer and the per-agent layer must report the MORE SPECIFIC origin
    (``per-agent``), not the least-specific one a naive first-occurrence
    walk over ``all_defs()`` (startup-first order) would pick."""
    monkeypatch.chdir(tmp_path)
    shared_name = "shared-hook-name"
    s = _make_session(tmp_path, hooks_config=[
        {
            "on": "turn_end",
            "name": shared_name,
            "template_push": {"message": "startup copy", "wake": True},
        },
    ])
    per_agent_hooks_path = tmp_path / ".reyn" / "agents" / "alice" / "hooks.yaml"
    per_agent_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    per_agent_hooks_path.write_text(
        yaml.safe_dump({
            "hooks": [
                {
                    "on": "turn_end",
                    "name": shared_name,
                    "template_push": {"message": "per-agent copy", "wake": True},
                },
            ],
        }),
        encoding="utf-8",
    )
    await s._reapply_hooks({})

    state = {h["name"]: h for h in s.hook_state()}
    assert state[shared_name]["scope"] == "per-agent", (
        "the most-specific origin for a repeated name must win — a "
        "first-occurrence walk over the least-to-most-specific order "
        "would wrongly report 'startup'"
    )


@pytest.mark.asyncio
async def test_strip_the_origin_aware_enabled_check_reproduces_the_stale_display(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: strip-falsify — reconstructing the OLD bare name-membership
    ``enabled`` computation (mirroring pre-#5222) against the same real
    session/dispatcher must reproduce the misleading ``enabled: False`` for
    a hook that is actually still firing.

    #5230: ``set_hook_enabled`` itself now refuses to add a protected
    hook's name to ``_disabled_hooks`` at all, so seeding it directly here
    (bypassing ``set_hook_enabled``) isolates the #5222 DISPLAY regression
    this test targets from the separate #5230 write-refusal behavior."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)
    s._disabled_hooks.add(_STARTUP_HOOK_NAME)

    def _old_hook_state(self) -> "list[dict]":
        out = []
        seen = set()
        for hook in self._hook_dispatcher.registry.all_defs():
            n = hook.name
            if n is None or n in seen:
                continue
            seen.add(n)
            out.append({
                "name": n,
                "scope": hook.origin,
                "enabled": n not in self._disabled_hooks,
            })
        return out

    import types
    s.hook_state = types.MethodType(_old_hook_state, s)

    state = {h["name"]: h for h in s.hook_state()}
    assert state[_STARTUP_HOOK_NAME]["enabled"] is False, (
        "the OLD bare name-membership check must reproduce the stale, "
        "misleading display — if this assertion fails, the strip did not "
        "actually revert to the pre-#5222 shape"
    )
