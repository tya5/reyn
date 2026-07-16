"""Tier 1/2: the skill `visibility` axis + the `skill_list` discovery surface (#2971).

Before #2971 a skill outside the L1 system-prompt menu was unreachable, not
merely unadvertised: the menu was the ONLY surface that named a skill, and
`auto_invoke: false` removed the entry from exactly that surface. Builtin
skills — force-stamped `auto_invoke=False` by A3 — were therefore dead code.

The fix is a third STATE, not a second axis: `visibility: menu | on_demand |
hidden`, plus `skill_list` as the surface `on_demand` is reachable through.
This module pins the four contracts that make that true:

  1. **`hidden` never leaves `skill_list`.** The bound case puts a `hidden`
     skill OUTSIDE the boundary (a listing with no hidden entry present would
     pass whether or not the filter exists), alongside `menu` + `on_demand`
     entries that must survive.
  2. **`auto_invoke` is rejected at LOAD, with the operator's exact
     replacement.** The clean break is only decision-enabling if the error
     names the specific target: `false → hidden`, `true → menu`. The `false`
     case is the load-bearing one — it maps to today's BEHAVIOR (invisible to
     the model), not to the doc's old wording ("excluded from the menu",
     which now reads as `on_demand`).
  3. **`enabled` dominates `visibility`.** `enabled: false` drops the entry at
     registry build regardless of visibility, so the pair spans 4 states, not
     2x3=6.
  4. **Reachability, end-to-end, through real dispatch.** A builtin skill is
     discoverable via `skill_list` and its body is readable via the REAL `file`
     read op at the path `skill_list` handed back — the actor/path question A3
     was never asked. No hand-built data stands in for the wiring.

Falsify coverage: each load-bearing mechanism here (the hidden filter, the
load-time `auto_invoke` rejection, the SP menu's visibility predicate) was
strip-falsified against the working tree — removed, confirmed RED, restored —
with the evidence recorded in the PR's Test plan. Those strips cannot live in
the suite itself without mock-patching production code, which the testing
policy forbids; what IS pinned here are the contracts, plus regrounding cases
that prove the fixtures are not vacuous.

No mocks: real `load_config` over real temp config files, the real registered
`ToolDefinition`, and the real `file` read op handler.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from reyn.builtin.registry import build_builtin_config
from reyn.config.loader import load_config
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle as file_handle
from reyn.data.skills.registry import SkillEntry, build_skill_registry
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext


def _run(coro):
    return asyncio.run(coro)


def _tool_ctx(skills: "list[SkillEntry] | None") -> ToolContext:
    events = EventLog()
    return ToolContext(
        events=events,
        permission_resolver=None,
        workspace=Workspace(events=events),
        caller_kind="router",
        router_state=RouterCallerState(available_skills=skills),
    )


def _skill_list(skills: "list[SkillEntry] | None") -> "list[dict]":
    """Invoke the REAL registered skill_list ToolDefinition's handler."""
    tool = get_default_registry().lookup("skill_list")
    assert tool is not None, "skill_list is not registered"
    result = _run(tool.handler({}, _tool_ctx(skills)))
    return result["skills"]


def _write_config(root: Path, body: str) -> None:
    (root / "reyn.yaml").write_text(body, encoding="utf-8")


def _load_from(root: Path):
    old = os.getcwd()
    os.chdir(root)
    try:
        return load_config()
    finally:
        os.chdir(old)


# ── 1. hidden never leaves skill_list ────────────────────────────────────────


def test_skill_list_returns_menu_and_on_demand_but_never_hidden() -> None:
    """Tier 1: skill_list returns every non-hidden skill and excludes hidden.

    The `hidden` entry is deliberately PRESENT in the input — the boundary is
    only exercised when something sits outside it.
    """
    listed = _skill_list([
        SkillEntry(name="in_menu", description="d", path="p1", visibility="menu"),
        SkillEntry(name="on_demand_one", description="d", path="p2", visibility="on_demand"),
        SkillEntry(name="never", description="d", path="p3", visibility="hidden"),
    ])

    names = {s["name"] for s in listed}
    assert names == {"in_menu", "on_demand_one"}, (
        "skill_list must return menu + on_demand and never hidden"
    )


def test_skill_list_hidden_exclusion_is_not_vacuous() -> None:
    """Tier 1: (regrounding) the hidden skill really is in skill_list's input
    and really is enabled — so the exclusion above is the filter's doing and not
    an artifact of a fixture that never presented a hidden entry at all.

    This regrounds the fixture; it does not strip the mechanism. The actual
    strip-falsify (delete the visibility predicate in `skill_verbs._handle_
    skill_list` -> the exclusion test goes RED) was run against the working
    tree and is recorded in the PR's Test plan — an in-suite test cannot strip
    its own production code without mock-patching it, which the testing policy
    forbids.
    """
    entries = [
        SkillEntry(name="in_menu", description="d", path="p1", visibility="menu"),
        SkillEntry(name="never", description="d", path="p3", visibility="hidden"),
    ]

    assert {e.name for e in entries if e.enabled} == {"in_menu", "never"}, (
        "fixture invariant: both entries are enabled and reach the filter"
    )
    assert "never" not in {s["name"] for s in _skill_list(entries)}


def test_skill_list_returns_name_description_and_path() -> None:
    """Tier 1: each listed skill carries the three fields a caller needs — path
    above all, since reading it IS the invocation (there is no run verb)."""
    listed = _skill_list([
        SkillEntry(name="s", description="what it does", path="skills/s/SKILL.md"),
    ])

    assert listed == [
        {"name": "s", "description": "what it does", "path": "skills/s/SKILL.md"},
    ]


def test_skill_list_empty_when_no_skills_registered() -> None:
    """Tier 1: no registry (a context with no project root) is an empty list,
    not an error — skill_list is a discovery surface, not a gate."""
    assert _skill_list(None) == []


# ── 2. auto_invoke is rejected at load, with the exact replacement ───────────


def test_load_rejects_auto_invoke_false_and_names_hidden(tmp_path: Path) -> None:
    """Tier 1: `auto_invoke: false` fails the load and the error names
    `visibility: hidden` — the state that preserves what the operator gets
    TODAY (invisible to the model), not the `on_demand` its old doc line's
    wording ("excluded from the menu") would suggest."""
    _write_config(tmp_path, (
        "skills:\n"
        "  entries:\n"
        "    quiet:\n"
        "      path: skills/quiet/SKILL.md\n"
        "      description: d\n"
        "      auto_invoke: false\n"
    ))

    with pytest.raises(ValueError) as exc:
        _load_from(tmp_path)

    message = str(exc.value)
    assert "auto_invoke" in message
    assert "visibility: hidden" in message, (
        f"the error must name the exact replacement, got: {message}"
    )
    assert "quiet" in message, "the error must name the offending entry"


def test_load_rejects_auto_invoke_true_and_names_menu(tmp_path: Path) -> None:
    """Tier 1: the mapping is per-VALUE — `auto_invoke: true` maps to
    `visibility: menu`. A single-value check would pass a hardcoded reply."""
    _write_config(tmp_path, (
        "skills:\n"
        "  entries:\n"
        "    loud:\n"
        "      path: skills/loud/SKILL.md\n"
        "      description: d\n"
        "      auto_invoke: true\n"
    ))

    with pytest.raises(ValueError) as exc:
        _load_from(tmp_path)

    message = str(exc.value)
    assert "visibility: menu" in message, (
        f"the error must name the exact replacement, got: {message}"
    )
    assert "visibility: hidden" not in message


def test_load_rejects_an_unknown_visibility_value(tmp_path: Path) -> None:
    """Tier 1: the enum is closed — a typo fails at load naming the legal set,
    rather than silently degrading to the default."""
    _write_config(tmp_path, (
        "skills:\n"
        "  entries:\n"
        "    typo:\n"
        "      path: skills/typo/SKILL.md\n"
        "      description: d\n"
        "      visibility: on-demand\n"
    ))

    with pytest.raises(ValueError) as exc:
        _load_from(tmp_path)

    message = str(exc.value)
    assert "on-demand" in message
    for legal in ("menu", "on_demand", "hidden"):
        assert legal in message, f"the error must name the legal value {legal!r}"


@pytest.mark.parametrize("value", ["menu", "on_demand", "hidden"])
def test_load_accepts_every_declared_visibility_value(tmp_path: Path, value: str) -> None:
    """Tier 1: each of the three states round-trips from config to registry —
    including the non-default ones, which a default-only check would not catch."""
    _write_config(tmp_path, (
        "skills:\n"
        "  entries:\n"
        "    s:\n"
        "      path: skills/s/SKILL.md\n"
        "      description: d\n"
        f"      visibility: {value}\n"
    ))

    cfg = _load_from(tmp_path)
    entries = build_skill_registry(cfg.skills)

    assert [e.visibility for e in entries if e.name == "s"] == [value]


# ── 3. enabled dominates visibility ─────────────────────────────────────────


@pytest.mark.parametrize("visibility", ["menu", "on_demand", "hidden"])
def test_enabled_false_dominates_every_visibility(visibility: str) -> None:
    """Tier 1: `enabled: false` drops the entry at registry build whatever its
    visibility says — so the two fields describe 4 reachable states, not 6, and
    visibility is meaningful only while enabled."""
    entries = build_skill_registry({
        "entries": {
            "off": {
                "path": "p", "description": "d",
                "enabled": False, "visibility": visibility,
            },
        },
    })

    assert entries == [], (
        f"enabled: false must drop the entry regardless of visibility={visibility!r}"
    )


def test_enabled_true_keeps_the_entry_for_every_visibility() -> None:
    """Tier 1: the complement of the case above — enabled entries survive
    registry build in all three states (the SP menu and skill_list, not the
    registry, are where visibility discriminates)."""
    entries = build_skill_registry({
        "entries": {
            "a": {"path": "p", "description": "d", "visibility": "menu"},
            "b": {"path": "p", "description": "d", "visibility": "on_demand"},
            "c": {"path": "p", "description": "d", "visibility": "hidden"},
        },
    })

    assert {e.name for e in entries} == {"a", "b", "c"}


# ── 4. Reachability, end-to-end, through real dispatch ──────────────────────


def test_builtin_skills_ship_on_demand_and_are_listed() -> None:
    """Tier 2: every skill the builtin tier SHIPS is on_demand, and skill_list
    surfaces it — the hop whose absence made builtin skills dead code.

    Layer (a) of the reachability assert: the actor is the model, and the path
    is skill_list.

    Scope note (verified by strip-falsify, do not overstate): this pins the
    shipped RESULT, not A3's force. Stripping the force-stamp leaves this test
    green, because `BUILTIN_SKILLS` also declares `on_demand` in source. The
    force itself — an entry declaring otherwise is overridden anyway — is
    witnessed by `test_0060_phase1_f3a_builtin_tier.py::
    test_builtin_skill_entry_is_inert_by_construction`, which declares `menu`
    in source and goes RED when the stamp is removed. Both are needed: that one
    proves the override happens, this one proves what actually ships.
    """
    cfg = build_builtin_config()
    entries = build_skill_registry(cfg["skills"])

    assert entries, "fixture invariant: the builtin tier ships at least one skill"
    assert {e.visibility for e in entries} == {"on_demand"}, (
        "every builtin skill must ship on_demand (A3, as corrected by #2971)"
    )

    listed = {s["name"] for s in _skill_list(entries)}
    for entry in entries:
        assert entry.name in listed, (
            f"builtin skill {entry.name!r} is registered but skill_list does not "
            f"return it — it would be unreachable, the #2971 defect"
        )


def test_builtin_skill_body_is_readable_at_the_path_skill_list_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the whole chain, through real dispatch — skill_list yields a
    path, and the REAL `file` read op returns that skill's body from it, with
    project_root somewhere else entirely (the wheel-install fact) and no
    operator present to approve anything.

    Layer (b) of the reachability assert: a listed skill can actually be acted
    on. This is what makes "on_demand" mean quiet rather than unreachable, and
    it is why #2971 adds no run verb — reading the body IS the invocation.
    """
    monkeypatch.chdir(tmp_path)
    unrelated_root = tmp_path / "unrelated_project"
    unrelated_root.mkdir()

    cfg = build_builtin_config()
    entries = build_skill_registry(cfg["skills"])
    listed = _skill_list(entries)
    assert listed, "fixture invariant: builtin skills are listed"

    resolver = PermissionResolver(
        config_permissions={}, project_root=unrelated_root, interactive=False,
    )
    events = EventLog()
    ctx = OpContext(
        workspace=Workspace(events=events),
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test_2971",
    )

    for skill in listed:
        assert not str(Path(skill["path"]).resolve()).startswith(
            str(unrelated_root.resolve())
        ), "fixture invariant: the builtin path is genuinely outside project_root"

        result = _run(file_handle(
            FileIROp(kind="file", op="read", path=skill["path"]), ctx,
        ))

        assert result["status"] == "ok", (
            f"skill_list returned {skill['path']!r} but the file read op could not "
            f"read it — the model would be handed an unusable path: {result}"
        )
        # Behavioral, not a golden pin: the body is this skill's own front-matter.
        assert skill["name"] in result["content"]


# ── registry-completeness: the new tool is wired on every seam ──────────────


def test_skill_list_is_registered_and_routed() -> None:
    """Tier 1: skill_list is reachable by BOTH the surfaces a router tool needs
    — the registry (bare call) and the universal-catalog route (invoke_action).
    A tool wired to one but not the other is the "registered but LLM-invisible"
    bug class #2971 itself is an instance of."""
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    assert get_default_registry().lookup("skill_list") is not None
    assert _OPERATION_RULES["skill_management__list"][0] == "skill_list"


def test_skill_list_is_enumerated_in_its_category() -> None:
    """Tier 1: list_actions(category=["skill_management"]) surfaces skill_list.

    The gap this closes is precedented in this very category: skill_management
    was dispatch-wired but absent from the enumeration list, so its verbs were
    invocable yet invisible (the comment in universal_catalog records it).
    """
    from reyn.tools.universal_catalog import _enumerate_static_category

    names = {a["qualified_name"] for a in _enumerate_static_category("skill_management")}
    assert "skill_management__list" in names
