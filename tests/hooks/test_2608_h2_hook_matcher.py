"""Tests for #2608 H2 — ``HookDef.matcher`` interpretation (filter external events).

H1 added the first EXTERNAL-event hook-point, ``mcp_resource_updated``, but a
hook on it fired for ANY subscribed-resource update — scoping was only via
which resources were subscribed. H2 interprets the (previously reserved/
uninterpreted) ``matcher`` field so a hook can filter WHICH events it fires
on: a ``dict[str, str]`` of field -> pattern, evaluated against the event's
``template_vars`` BEFORE the hook's action runs. For ``mcp_resource_updated``:
``server`` matches exactly, ``uri`` matches via a shell-style glob.

Coverage plan
-------------
Tier 1 (contract): loader validation — a dict matcher round-trips through
  ``load_hooks``; a malformed matcher (non-dict, non-string key/value) raises
  ``HookConfigError`` at load time.
Tier 1 (contract): ``reyn.hooks.matcher.matches`` — the pure predicate, unit
  tested directly (server exact, uri glob, absent-field, empty/None matcher).
Tier 2 (OS invariant, dispatcher-unit): ``HookDispatcher.dispatch`` skips a
  non-matching hook's action entirely (never reaches ``_dispatch_one``) and
  runs a matching hook's action, driven through the REAL dispatcher + REAL
  registry loaded via the REAL ``load_hooks`` seam, observed via the action
  seam (a real recording async callable — no mock). Also covers: a hook with
  NO matcher fires for every event (byte-identical to H1), and a lifecycle
  hook (no external payload fields) with no matcher is unaffected.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import load_hooks
from reyn.hooks.matcher import matches
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookConfigError, HookDef

# ---------------------------------------------------------------------------
# Recording seam (mirrors test_2608_h3_pipeline_launch_hook.py's _Recorder)
# ---------------------------------------------------------------------------


class _Recorder:
    """A real recording async callable — generic seam stand-in for
    ``put_inbox``/``stage_next_turn_context`` (accepts any args/kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _dispatcher(hooks: list[HookDef]) -> "tuple[HookDispatcher, _Recorder]":
    put_inbox = _Recorder()
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=put_inbox,
        stage_next_turn_context=_Recorder(),
    )
    return disp, put_inbox


def _mcp_vars(*, server: str = "github", uri: str = "file:///repo/a.txt") -> dict:
    return {
        "point": "mcp_resource_updated",
        "server": server,
        "uri": uri,
        "agent_name": "test-agent",
        "resync": False,
    }


# ===========================================================================
# Tier 1 — Contract: loader validation
# ===========================================================================


def test_matcher_dict_round_trips_through_loader() -> None:
    """Tier 1: a ``matcher:`` dict on an ``mcp_resource_updated`` hook parses
    through the REAL ``load_hooks`` seam unchanged."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "{{ uri }} updated"},
            "matcher": {"server": "github", "uri": "file:///repo/**"},
        },
    ]
    registry = load_hooks(raw)
    (hook,) = registry.hooks_for("mcp_resource_updated")
    assert hook.matcher == {"server": "github", "uri": "file:///repo/**"}


def test_matcher_absent_stays_none() -> None:
    """Tier 1: no ``matcher:`` key -> ``HookDef.matcher is None`` (fire-always)."""
    raw = [{"on": "turn_end", "exec": ["echo", "hi"]}]
    registry = load_hooks(raw)
    (hook,) = registry.hooks_for("turn_end")
    assert hook.matcher is None


def test_matcher_empty_dict_normalises_to_none() -> None:
    """Tier 1: an empty ``matcher: {}`` normalises to ``None`` (same fire-always
    semantics as absent — no functional difference the operator should rely on
    a distinction for)."""
    raw = [{"on": "turn_end", "exec": ["echo", "hi"], "matcher": {}}]
    registry = load_hooks(raw)
    (hook,) = registry.hooks_for("turn_end")
    assert hook.matcher is None


@pytest.mark.parametrize(
    "bad_matcher",
    [
        "a-plain-string",  # H1-era reserved-string shape no longer accepted
        ["server", "github"],
        {"server": 123},
        {1: "github"},
        {"": "github"},
    ],
)
def test_malformed_matcher_raises_hook_config_error(bad_matcher) -> None:
    """Tier 1: a malformed matcher (non-dict, non-string key/value, or an empty
    key) raises ``HookConfigError`` at load time — decision-enabling, not a
    silent ignore."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "x"},
            "matcher": bad_matcher,
        },
    ]
    with pytest.raises(HookConfigError):
        load_hooks(raw)


# ===========================================================================
# Tier 1 — Contract: reyn.hooks.matcher.matches — the pure predicate
# ===========================================================================


def test_matches_none_matcher_always_true() -> None:
    """Tier 1: no matcher -> always fires."""
    assert matches(None, {}) is True
    assert matches(None, _mcp_vars()) is True


def test_matches_empty_matcher_always_true() -> None:
    """Tier 1: an empty matcher dict -> always fires (same as None)."""
    assert matches({}, _mcp_vars()) is True


def test_matches_server_exact() -> None:
    """Tier 1: ``server`` matches by exact string equality only."""
    assert matches({"server": "github"}, _mcp_vars(server="github")) is True
    assert matches({"server": "github"}, _mcp_vars(server="gitlab")) is False
    assert matches({"server": "git*"}, _mcp_vars(server="github")) is False  # no glob for server


def test_matches_uri_glob() -> None:
    """Tier 1: ``uri`` matches via a shell-style glob (fnmatch)."""
    assert matches({"uri": "file:///repo/**"}, _mcp_vars(uri="file:///repo/a.txt")) is True
    assert matches({"uri": "file:///repo/*.txt"}, _mcp_vars(uri="file:///repo/a.txt")) is True
    assert matches({"uri": "file:///repo/*.txt"}, _mcp_vars(uri="file:///other/a.txt")) is False
    assert matches({"uri": "file:///repo/*.md"}, _mcp_vars(uri="file:///repo/a.txt")) is False


def test_matches_multiple_fields_all_must_match() -> None:
    """Tier 1: a multi-field matcher requires EVERY named field to match."""
    matcher = {"server": "github", "uri": "file:///repo/**"}
    assert matches(matcher, _mcp_vars(server="github", uri="file:///repo/a.txt")) is True
    # server matches, uri doesn't -> overall no match
    assert matches(matcher, _mcp_vars(server="github", uri="file:///other/a.txt")) is False
    # uri matches, server doesn't -> overall no match
    assert matches(matcher, _mcp_vars(server="gitlab", uri="file:///repo/a.txt")) is False


def test_matches_field_absent_from_template_vars_is_no_match() -> None:
    """Tier 1: a matcher naming a field that ``template_vars`` doesn't carry
    (e.g. a lifecycle hook's vars have no ``server``/``uri``) never matches —
    a matcher can only narrow, never invent an unfired signal."""
    assert matches({"server": "github"}, {"point": "turn_end"}) is False


# ===========================================================================
# Tier 2 — OS invariant: HookDispatcher applies the matcher before dispatch
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatch_skips_hook_whose_matcher_server_does_not_match() -> None:
    """Tier 2: a hook with ``matcher: {server: X}`` fires for server=X, SKIPS
    server=Y — driven through the REAL dispatcher + REAL registry."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "{{ uri }} updated"},
            "matcher": {"server": "github"},
        },
    ]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="gitlab"))
    assert recorder.calls == []  # skipped — server didn't match

    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="github"))
    (_,) = recorder.calls  # exactly one call — fired, server matched


@pytest.mark.asyncio
async def test_dispatch_uri_glob_match_and_no_match() -> None:
    """Tier 2: a hook with ``matcher: {uri: glob}`` fires on a matching URI and
    is skipped on a non-matching one."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "{{ uri }} updated"},
            "matcher": {"uri": "file:///repo/**"},
        },
    ]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("mcp_resource_updated", _mcp_vars(uri="file:///other/a.txt"))
    assert recorder.calls == []

    await disp.dispatch("mcp_resource_updated", _mcp_vars(uri="file:///repo/a.txt"))
    (_,) = recorder.calls  # exactly one call — fired, uri matched the glob


@pytest.mark.asyncio
async def test_dispatch_no_matcher_fires_for_every_event() -> None:
    """Tier 2: a hook with NO matcher fires for every event on its point —
    byte-identical to pre-H2 (H1) behavior."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "{{ uri }} updated"},
        },
    ]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="github"))
    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="gitlab"))
    (_, _) = recorder.calls  # exactly two calls — both fired, no matcher to filter on


@pytest.mark.asyncio
async def test_dispatch_lifecycle_hook_with_no_matcher_unaffected() -> None:
    """Tier 2: a lifecycle hook (no external-event payload fields at all) with
    no matcher configured is completely unaffected by H2 — fires exactly as
    it did pre-H2 on its point's (lifecycle) ``template_vars``."""
    raw = [{"on": "turn_end", "template_push": {"message": "turn done"}}]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("turn_end", {})
    (_,) = recorder.calls  # exactly one call — fired unchanged


@pytest.mark.asyncio
async def test_dispatch_matching_and_nonmatching_hooks_are_isolated_siblings() -> None:
    """Tier 2: two hooks on the same point, one matching and one not — the
    matching one fires, the non-matching one is silently skipped, and neither
    affects the other (per-hook isolation, same as the disabled-hook gate)."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "name": "github-only",
            "template_push": {"message": "github: {{ uri }}"},
            "matcher": {"server": "github"},
        },
        {
            "on": "mcp_resource_updated",
            "name": "gitlab-only",
            "template_push": {"message": "gitlab: {{ uri }}"},
            "matcher": {"server": "gitlab"},
        },
    ]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="github"))
    (call,) = recorder.calls  # exactly one call — the matching hook fired, its sibling didn't
    _, payload = call[0]
    assert payload["text"] == "github: file:///repo/a.txt"
