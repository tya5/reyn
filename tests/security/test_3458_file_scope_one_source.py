"""Tier 2: #3458 — one source for "which paths are readable / writable".

Acceptance ③ (the N+1 gate) is the body of this file. A **third** caller —
:class:`_ThirdSubsystem`, which knows only ``(config, zone_root)`` and the
public :func:`resolve_file_scope` — answers both questions the OS asks about a
path set:

- *membership*    "is this path in the set?"   (what the runtime gate needs)
- *enumeration*   "what is the set?"           (what the advertisement needs)

Every assertion compares the runtime gate's answer and the advertisement's
answer against that third answer. **No expected value is written by hand** —
the oracle is the resolution function, so a fourth subsystem asking the same
question inherits the guarantee instead of adding a place to keep in sync
(#3376's lesson: a hand-written expectation pins today's answer, not the
structure).

Falsification measured while writing this file (see the PR body): reverting
``AgentLayer.allows`` to the pre-#3458 ``_in_default_read_zone(...)`` call —
i.e. giving the gate its own source again while leaving the advertisement on
the resolver — turns :func:`test_three_answers_agree_on_membership` RED on the
``read-list-narrows`` and ``read-list-extends`` configs.

Acceptance ④ (JIT unchanged) is pinned at the bottom: outside the configured
set, a bus still asks and a missing bus still denies.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.intervention_choices import YES
from reyn.runtime.router_tools import build_tools
from reyn.security.permissions.file_scope import (
    FileScopeAxis,
    resolve_file_scope,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _ThirdSubsystem:
    """The N+1 caller: it holds no resolver and no layer stack — only the
    operator's ``permissions`` dict, the environment's zone anchor, and the one
    public resolution function."""

    def __init__(self, config: dict, zone_root: Path) -> None:
        self._config = config
        self._zone_root = zone_root

    def _scope(self, axis: FileScopeAxis):
        return resolve_file_scope(self._config, axis, zone_root=self._zone_root)

    def readable(self, path: str) -> bool:
        return self._scope(FileScopeAxis.READ).contains(path)

    def writable(self, path: str) -> bool:
        return self._scope(FileScopeAxis.WRITE).contains(path)

    def advertised(self) -> dict:
        read = self._scope(FileScopeAxis.READ)
        write = self._scope(FileScopeAxis.WRITE)
        if read.is_empty and write.is_empty:
            return {}
        return {
            "read": list(read.advertised_paths),
            "write": list(write.advertised_paths),
        }


class _FakeBus:
    """RequestBus-compatible fake answering with a scripted choice (no mocks)."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _configs(zone: Path) -> dict[str, dict]:
    """The four config forms of the axis, plus the nested spelling."""
    return {
        "unset": {},
        "read-deny": {"file.read": "deny"},
        "read-allow": {"file.read": "allow"},
        "read-list-narrows": {"file.read": ["docs"]},
        "read-list-extends": {"file.read": [str(zone.parent / "outside")]},
        "read-list-nested-spelling": {"file": {"read": ["docs"]}},
        "write-list": {"file.write": ["out"]},
        "write-deny": {"file.write": "deny"},
        "write-allow": {"file.write": "allow"},
        "both-empty": {"file.read": "deny", "file.write": "deny"},
    }


def _probe_paths(zone: Path) -> list[str]:
    return [
        str(zone / "docs" / "a.md"),
        str(zone / "src" / "a.py"),
        str(zone / ".reyn" / "scratch.json"),
        str(zone / ".reyn" / "approvals.yaml"),
        str(zone / "out" / "r.txt"),
        str(zone.parent / "outside" / "x.txt"),
    ]


def _gate_readable(resolver: PermissionResolver, path: str) -> bool:
    """The runtime gate's answer. ``bus=None`` so the JIT layer cannot mask the
    set membership question this test is asking."""
    try:
        asyncio.run(
            resolver.require_file_read(PermissionDecl(), path, "actor_x", bus=None),
        )
    except PermissionError:
        return False
    return True


def _gate_writable(resolver: PermissionResolver, path: str) -> bool:
    try:
        asyncio.run(
            resolver.require_file_write(PermissionDecl(), path, "actor_x", bus=None),
        )
    except PermissionError:
        return False
    return True


# ── ③ the N+1 gate ───────────────────────────────────────────────────────────


def test_three_answers_agree_on_membership(tmp_path: Path) -> None:
    """Tier 2: #3458 ③ — for every config form × probe path, the runtime gate,
    the resolver the advertisement is built from, and a third subsystem that
    knows only (config, zone_root) give the SAME membership answer."""
    zone = tmp_path / "proj"
    zone.mkdir()
    checked = 0
    for name, config in _configs(zone).items():
        resolver = PermissionResolver(config, project_root=zone)
        third = _ThirdSubsystem(config, zone)
        for path in _probe_paths(zone):
            gate_r = _gate_readable(resolver, path)
            gate_w = _gate_writable(resolver, path)
            advertised_source = resolver.file_scopes()
            assert gate_r is third.readable(path) is advertised_source.read.contains(path), (
                f"{name}: read answers disagree for {path}"
            )
            assert gate_w is third.writable(path) is advertised_source.write.contains(path), (
                f"{name}: write answers disagree for {path}"
            )
            checked += 1
    assert checked > 0


def test_three_answers_agree_on_the_advertised_set(tmp_path: Path) -> None:
    """Tier 2: #3458 ② — the set the model is TOLD about is the set the gate
    enforces: the advertisement surface and the third subsystem enumerate
    identically, and every advertised concrete path is one the gate admits."""
    zone = tmp_path / "proj"
    zone.mkdir()
    for name, config in _configs(zone).items():
        resolver = PermissionResolver(config, project_root=zone)
        third = _ThirdSubsystem(config, zone)
        advertised = resolver.advertised_file_permissions() or {}
        assert advertised == third.advertised(), f"{name}: advertised sets disagree"
        for axis_key, gate in (("read", _gate_readable), ("write", _gate_writable)):
            for advertised_path in advertised.get(axis_key, []):
                if advertised_path == "*":
                    continue
                probe = str(Path(advertised_path) if Path(advertised_path).is_absolute()
                            else zone / advertised_path)
                assert gate(resolver, probe) is True, (
                    f"{name}: advertised {axis_key} path {probe} is denied by the gate"
                )


def test_unconfigured_project_advertises_the_file_tools(tmp_path: Path) -> None:
    """Tier 2: #3458 / #3449 — with an empty permissions config the gate grants
    reads under the zone root, so the read tools must be advertised. Pre-#3458
    the advertisement saw only the (empty) config and withheld them, leaving the
    model unaware of a capability it had."""
    zone = tmp_path / "proj"
    zone.mkdir()
    resolver = PermissionResolver({}, project_root=zone)
    names = {
        t["function"]["name"]
        for t in build_tools(file_permissions=resolver.advertised_file_permissions())
    }
    assert {"read_file", "list_directory"} <= names
    assert _gate_readable(resolver, str(zone / "src" / "a.py")) is True


def test_deny_hides_the_file_tools(tmp_path: Path) -> None:
    """Tier 2: #3458 — ``deny`` on both axes is the empty set: nothing is
    advertised and the gate refuses, from the same resolution."""
    zone = tmp_path / "proj"
    zone.mkdir()
    config = {"file.read": "deny", "file.write": "deny"}
    resolver = PermissionResolver(config, project_root=zone)
    assert resolver.advertised_file_permissions() is None
    names = {
        t["function"]["name"]
        for t in build_tools(file_permissions=resolver.advertised_file_permissions())
    }
    assert not ({"read_file", "list_directory", "write_file", "delete_file"} & names)
    assert _gate_readable(resolver, str(zone / "src" / "a.py")) is False


def test_zone_symbol_resolves_against_the_environment_anchor(tmp_path: Path) -> None:
    """Tier 2: #3458 — the schema default is a SYMBOL, not a literal: the same
    unset config resolves to different concrete sets under different zone
    anchors (chat/web pass ws_base_dir, pipe/plugin pass project_root)."""
    host = tmp_path / "host"
    container = tmp_path / "testbed"
    host.mkdir()
    container.mkdir()
    resolver = PermissionResolver({}, project_root=host, file_zone_root=container)
    third_at_container = _ThirdSubsystem({}, container)
    third_at_host = _ThirdSubsystem({}, host)
    probe = str(container / "repo" / "a.py")
    assert _gate_readable(resolver, probe) is third_at_container.readable(probe) is True
    assert third_at_host.readable(probe) is False


# ── #3925 ①-a: the "<zone-root>" spelling on a WRITE-axis list ──────────────


def test_zone_root_spelling_scopes_write_to_the_zone(tmp_path: Path) -> None:
    """Tier 2: #3925 — ``file.write: ["<zone-root>"]`` resolves to the SAME
    containment :class:`ZoneRoot` gives the READ axis by default (the literal
    string is the notation ``file_scope.py``'s own docstring already uses to
    describe :class:`ZoneRoot`'s resolved rendering), not a
    :class:`LiteralPath` naming a directory that happens to be spelled that
    way. A path under the zone is writable; a path outside is not — the
    resolved set is bounded, not the unrestricted ``allow`` form."""
    zone = tmp_path / "proj"
    zone.mkdir()
    resolver = PermissionResolver(
        {"file.write": ["<zone-root>"]}, project_root=zone,
    )
    inside = str(zone / "src" / "a.py")
    outside = str(tmp_path / "elsewhere" / "b.py")
    assert _gate_writable(resolver, inside) is True
    assert _gate_writable(resolver, outside) is False


def test_zone_root_spelling_matches_the_read_axis_default_containment(tmp_path: Path) -> None:
    """Tier 2: #3925 — non-vacuity + cross-axis agreement. The READ axis's
    schema DEFAULT is ``ZoneRoot()`` (unset config); an EXPLICIT
    ``file.write: ["<zone-root>"]`` must answer the SAME membership question
    for the SAME probes — confirming the symbol resolves correctly on the
    WRITE axis too (previously unmeasured: the WRITE axis's own default is
    ``ZoneStateDir()``, a different symbol, so nothing exercised ``ZoneRoot``
    there before this fix)."""
    zone = tmp_path / "proj"
    zone.mkdir()
    probes = [
        str(zone / "src" / "a.py"),
        str(zone / "docs" / "b.md"),
        str(tmp_path / "outside" / "c.py"),
    ]
    read_default = resolve_file_scope({}, FileScopeAxis.READ, zone_root=zone)
    write_zone_root = resolve_file_scope(
        {"file.write": ["<zone-root>"]}, FileScopeAxis.WRITE, zone_root=zone,
    )
    for p in probes:
        assert read_default.contains(p) is write_zone_root.contains(p)
    # non-vacuity: the two forms actually disagree with a bare "allow" grant,
    # which is what #3925 replaces "<zone-root>" as an alternative to.
    write_allow = resolve_file_scope(
        {"file.write": "allow"}, FileScopeAxis.WRITE, zone_root=zone,
    )
    outside = str(tmp_path / "outside" / "c.py")
    assert write_allow.contains(outside) is True
    assert write_zone_root.contains(outside) is False


def test_a_literal_path_that_is_not_the_zone_root_spelling_stays_literal(tmp_path: Path) -> None:
    """Tier 2: #3925 non-vacuity — a directory that merely CONTAINS angle
    brackets in its name (not the exact "<zone-root>" string) still parses as
    an ordinary :class:`LiteralPath`, not the symbol. Guards against an
    over-broad match (e.g. a substring check) silently swallowing an
    operator's real path."""
    zone = tmp_path / "proj"
    weird_dir = zone / "<zone-root>-backup"
    weird_dir.mkdir(parents=True)
    scope = resolve_file_scope(
        {"file.write": [str(weird_dir)]}, FileScopeAxis.WRITE, zone_root=zone,
    )
    assert scope.contains(str(weird_dir / "f.txt")) is True
    # And it does NOT grant the whole zone the way the real symbol would —
    # a sibling directory stays outside the (literal, narrow) grant.
    assert scope.contains(str(zone / "other" / "f.txt")) is False


# ── ④ the JIT layer is unchanged ─────────────────────────────────────────────


def test_jit_still_asks_outside_the_scope(tmp_path: Path) -> None:
    """Tier 2: #3458 ④ — a path outside the configured set still prompts when a
    bus is present, and the approval admits it."""
    zone = tmp_path / "proj"
    zone.mkdir()
    resolver = PermissionResolver({}, project_root=zone)
    outside = str(tmp_path / "elsewhere" / "notes.md")
    bus = _FakeBus(YES)
    asyncio.run(resolver.require_file_read(PermissionDecl(), outside, "actor_x", bus=bus))
    (ask,) = bus.asks
    assert ask.kind == "permission.file.read"


def test_jit_denies_without_a_bus_outside_the_scope(tmp_path: Path) -> None:
    """Tier 2: #3458 ④ — no bus, outside the set → deny (non-interactive)."""
    zone = tmp_path / "proj"
    zone.mkdir()
    resolver = PermissionResolver({}, project_root=zone)
    outside = str(tmp_path / "elsewhere" / "notes.md")
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_read(PermissionDecl(), outside, "actor_x", bus=None),
        )


def test_deny_suppresses_the_jit_ask(tmp_path: Path) -> None:
    """Tier 2: #3458 ④ — ``deny`` is not merely an empty set that JIT could
    re-extend; it still short-circuits before the prompt (pre-#3458 behaviour)."""
    zone = tmp_path / "proj"
    zone.mkdir()
    resolver = PermissionResolver({"file.read": "deny"}, project_root=zone)
    bus = _FakeBus(YES)
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver.require_file_read(
                PermissionDecl(), str(zone / "src" / "a.py"), "actor_x", bus=bus,
            ),
        )
    assert bus.asks == []
