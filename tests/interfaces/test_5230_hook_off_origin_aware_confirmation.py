"""Tier 2: #5230 — ``/hook off`` (and ``Session.set_hook_enabled`` beneath
it) must not confirm a disable it did not actually apply.

Root cause (architect ruling, issue #5230 — the operation-side companion to
#5227's display-side fix): a per-session ``disable`` request for a hook
whose most-specific origin is protected (``startup``/``runtime``, #5213)
used to be silently recorded into ``self._disabled_hooks`` and persisted
regardless, and ``/hook off``'s reply said "now disabled" unconditionally —
an ACTIVE false confirmation. Architect: this is worse than #5227's passive
display bug, because a caller who receives a confirmation does not go
verify the actual state afterward.

Fix: ``Session.set_hook_enabled`` returns a ``HookToggleResult`` (``applied:
bool``, ``origin: str | None``). A ``disable`` request whose hook's origin
is protected returns ``applied=False`` and changes NOTHING — the name is
never added to ``_disabled_hooks``, never persisted (closing lead-coder's
own concern: a protected-hook name sitting inert in a persisted
``disabled:`` list forever would read as "disabled but the hook fires
anyway" to a future operator inspecting the file by hand). ``/hook off``
reports the refusal, naming the hook's actual origin, instead of a generic
"cannot" with no reason.

Structural fix (architect: a census of every hook-enabled/disabled-
REPORTING surface cannot be closed with confidence — "a 4th could always
exist" — so collapse the predicate instead of enumerating and patching each
call site): ``Session._hook_effectively_disabled`` is now the ONE predicate
both ``hook_state()`` (#5227) and this fix's refusal decision derive from,
built directly on :func:`~reyn.hooks.schema.hook_origin_is_at_least_as_specific_as`
— the SAME function :class:`~reyn.hooks.dispatcher.HookDispatcher`'s own
``is_hook_disabled`` predicate calls. A stale doc paragraph in
``docs/reference/runtime/session-construction.md`` describing ``disabled:``
as a bare name-match (pre-#5213, never updated) is fixed in this same PR
(CLAUDE.md: a doc describing a mechanism is stale the moment the mechanism
changes — the doc IS one of the 3 known reporting surfaces architect asked
about).

Real ``Session``/``HookDispatcher`` — no mocks, mirrors
``test_5213_hook_disable_layer_bypass.py``'s own real-seam pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.hook import hook_cmd
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx

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
async def test_disabling_a_protected_hook_is_refused_not_applied(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — the exact #5230 witness. Disabling a
    startup-origin hook via the public seam must be REFUSED: the return
    value says so, the disabled-set is untouched, and nothing is
    persisted."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)

    result = s.set_hook_enabled(_STARTUP_HOOK_NAME, False)

    assert result.applied is False
    assert result.origin == "startup"
    state = {h["name"]: h for h in s.hook_state()}
    assert state[_STARTUP_HOOK_NAME]["enabled"] is True, (
        "a refused disable must leave the public read model unchanged"
    )
    hooks_yaml = Path(s._snapshot_path).parent / "hooks.yaml"
    if hooks_yaml.is_file():
        data = yaml.safe_load(hooks_yaml.read_text(encoding="utf-8")) or {}
        assert _STARTUP_HOOK_NAME not in (data.get("disabled") or []), (
            "a refused disable must not persist the name either — a stale "
            "inert entry would read as 'disabled but still fires' to a "
            "future operator inspecting the file by hand"
        )


@pytest.mark.asyncio
async def test_hook_off_reports_the_refusal_with_the_actual_origin(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — ``/hook off`` on a protected hook must NOT reply
    "now disabled" (the exact active-false-confirmation defect #5230
    closes); it must report the refusal and name the real origin."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)
    ctx = slash_ctx(s)

    await hook_cmd(ctx, f"off {_STARTUP_HOOK_NAME}")

    assert ctx.transport.displayed, "the handler must reply"
    joined = " ".join(m.text for m in ctx.transport.displayed)
    assert "now disabled" not in joined, (
        "must never claim a refused disable succeeded"
    )
    assert "startup" in joined, "must name the actual origin in the refusal"

    # Corroborate against real dispatch: the hook must still fire.
    await s._hook_dispatcher.dispatch("turn_end", {})
    assert s.inbox.qsize() >= 1, "the startup hook must still have fired"


@pytest.mark.asyncio
async def test_enabling_a_hook_is_never_refused_even_for_a_protected_origin(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast — an ``enable`` request is NEVER
    refused, for any origin: discarding a name from the disabled-set can
    only restore a hook to its baseline, never grant new power."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)

    result = s.set_hook_enabled(_STARTUP_HOOK_NAME, True)

    assert result.applied is True
    assert result.origin == "startup"


@pytest.mark.asyncio
async def test_disabling_a_genuine_per_session_hook_is_still_applied(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast — the feature #5230 narrows still
    works for its genuine scope: a per-session-origin hook IS disableable,
    reported as applied."""
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

    result = s.set_hook_enabled("session-own-hook", False)

    assert result.applied is True
    assert result.origin == "per-session"
    state = {h["name"]: h for h in s.hook_state()}
    assert state["session-own-hook"]["enabled"] is False, (
        "a genuinely per-session-origin hook must be reflected as disabled "
        "via the public read model"
    )


@pytest.mark.asyncio
async def test_disabling_an_unknown_hook_name_is_still_applied(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: non-regression — a name that resolves to no ``HookDef`` in
    the current merged registry is treated as freely disableable (``origin
    is None``), matching pre-#5230 behavior for this case (and
    ``hook_origin_is_at_least_as_specific_as``'s own fail-open contract for
    an origin outside its declared vocabulary)."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=None)

    result = s.set_hook_enabled("no-such-hook", False)

    assert result.applied is True
    assert result.origin is None
    hooks_yaml = Path(s._snapshot_path).parent / "hooks.yaml"
    data = yaml.safe_load(hooks_yaml.read_text(encoding="utf-8")) or {}
    assert "no-such-hook" in (data.get("disabled") or []), (
        "an unknown-name disable is still persisted, matching pre-#5230 "
        "behavior for this case"
    )


@pytest.mark.asyncio
async def test_strip_the_refusal_reproduces_the_active_false_confirmation(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: strip-falsify — reconstructing the OLD unconditional
    ``set_hook_enabled`` (mirroring pre-#5230: always add + persist,
    regardless of origin) against the same real session must reproduce the
    active false confirmation: the name lands in the disabled-set even
    though the hook's origin is protected."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)

    def _old_set_hook_enabled(self, name: str, enabled: bool) -> None:
        if enabled:
            self._disabled_hooks.discard(name)
        else:
            self._disabled_hooks.add(name)
        self._persist_hook_disabled()

    import types
    s.set_hook_enabled = types.MethodType(_old_set_hook_enabled, s)

    s.set_hook_enabled(_STARTUP_HOOK_NAME, False)

    hooks_yaml = Path(s._snapshot_path).parent / "hooks.yaml"
    data = yaml.safe_load(hooks_yaml.read_text(encoding="utf-8")) or {}
    assert _STARTUP_HOOK_NAME in (data.get("disabled") or []), (
        "the OLD unconditional write must reproduce the pre-#5230 shape "
        "(the protected hook's name persisted, inert, into disabled:) — "
        "if this assertion fails, the strip did not actually revert"
    )


@pytest.mark.asyncio
async def test_the_threshold_is_one_shared_method_not_hand_copied(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #5233 review (lead-coder, e2e-coder's live-reproduced
    finding) — a FIRST version of this PR routed ``hook_state()`` and the
    dispatcher's own ``is_hook_disabled`` lambda through
    ``_hook_effectively_disabled``, but ``set_hook_enabled``'s
    write-refusal check called ``hook_origin_is_at_least_as_specific_as``
    DIRECTLY with its own copy of the ``"per-agent"`` threshold literal —
    a 3rd, independent copy that could silently diverge from the other
    two. Reproduces the exact method that caught it: mutate
    ``Session._hook_origin_is_disableable`` (the one remaining place the
    threshold constant lives) and confirm BOTH the write-side decision
    (``set_hook_enabled``) and the real dispatch outcome move together —
    if they were still two independent copies, only one side would
    change."""
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

    def _widened_threshold(self, origin: str) -> bool:
        from reyn.hooks.schema import hook_origin_is_at_least_as_specific_as
        return hook_origin_is_at_least_as_specific_as(origin, "runtime")

    import types
    s._hook_origin_is_disableable = types.MethodType(_widened_threshold, s)

    result = s.set_hook_enabled("runtime-own-hook", False)
    assert result.applied is True, (
        "the widened threshold must make the WRITE side accept a "
        "runtime-origin disable"
    )

    await s._hook_dispatcher.dispatch("turn_end", {})
    assert s.inbox.qsize() == 0, (
        "the SAME widened threshold must make the real DISPATCH honor the "
        "disable too — if this fires while the write above succeeded, the "
        "two sides are still independent copies, not one shared predicate"
    )
