"""Tier 1/2: #6061 -- the exec tool axis unwraps a wrapper binary (``env``
/ ``xargs`` / ``sh -c`` / ``timeout`` / ``nice`` / ``command``,
``exec_wrappers.py``'s own curated table) rather than checking only
``argv[0]``.

Tier 1 (``exec_wrappers.py``'s own pure extraction functions): every one of
the 12 concrete command lines #6061's own issue thread measured against
``origin/main``, driven through the real production functions.

Tier 2 (``exec_plan_policy.check_exec_plan_policy``, wired end to end): real
``OpContext`` + real ``ContextualPermission`` throughout, no mocks -- same
shape ``test_5838_stage3_exec_plan_policy.py`` already established.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.security.exec_plan import ExecSegment
from reyn.security.exec_plan_policy import _MAX_UNWRAP_DEPTH, check_exec_plan_policy
from reyn.security.exec_wrappers import (
    UNSUPPORTED_WRAPPERS,
    extract_inner_argv,
    is_registered_wrapper,
)
from reyn.security.permissions.effective import ContextualPermission
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import collect_events, settle

_REAL_PATH = os.environ.get("PATH")


# ── Tier 1: exec_wrappers.py's own extraction, the 12 measured forms ────


@pytest.mark.parametrize(
    "name,argv,expected_inner",
    [
        ("env", ("env", "ls"), ("ls",)),
        ("xargs", ("xargs", "ls"), ("ls",)),
        ("sh", ("sh", "-c", "ls"), ("ls",)),
        ("timeout", ("timeout", "5", "ls"), ("ls",)),
        ("nice", ("nice", "ls"), ("ls",)),
        ("command", ("command", "ls"), ("ls",)),
        ("env", ("env", "FOO=1", "ls"), ("ls",)),
        ("env", ("env", "-i", "ls"), ("ls",)),
        ("nice", ("nice", "-n", "5", "ls"), ("ls",)),
        (
            "timeout",
            ("timeout", "5", "rm", "-rf", "/tmp/x"),
            ("rm", "-rf", "/tmp/x"),
        ),
    ],
)
def test_extracts_inner_argv_for_each_measured_form(name, argv, expected_inner):
    """Tier 1: accept side -- 10 of #6061's own 12 measured command lines
    (the other 2, ``eval ls`` and ``xargs -n 1 ls``, are the deny-side
    witnesses below: ``eval`` is not a registered wrapper at all, and
    ``xargs -n 1`` is the documented "cannot extract" form)."""
    assert is_registered_wrapper(name)
    assert extract_inner_argv(name, argv) == expected_inner


def test_xargs_with_any_flag_refuses_extraction():
    """Tier 1: deny -- #6061's own measured ``xargs -n 1 ls`` matches
    Claude Code's own documented limit verbatim (competitive research,
    #6061 issue thread): stripping applies to BARE xargs only."""
    assert extract_inner_argv("xargs", ("xargs", "-n", "1", "ls")) is None


def test_eval_is_not_a_registered_wrapper():
    """Tier 1: deny -- ``eval`` resolves to no real binary (a shell
    builtin, ``which`` returns None per #6061's own measurement) and is
    deliberately absent from this table's initial version (the 6 names
    that DO resolve to real binaries)."""
    assert not is_registered_wrapper("eval")


@pytest.mark.parametrize(
    "name,argv",
    [
        ("env", ("env", "-u", "PATH", "ls")),  # -u takes a separate value
        ("nice", ("nice", "-99", "ls")),  # not -n, not old-style digits-only shape recognised here... see below
        ("command", ("command", "-v", "ls")),
        ("sh", ("sh", "script.sh")),  # no -c at all
        ("timeout", ("timeout", "-s", "KILL", "5", "ls")),  # -s takes a value
    ],
)
def test_refuses_extraction_for_unsupported_flag_forms(name, argv):
    """Tier 1: deny -- each of this table's own documented
    ``unsupported_forms`` refuses rather than guesses. ``nice -99 ls`` IS
    actually the old-style adjustment shape and DOES extract (kept in
    this parametrize as a reminder it is covered, not a refusal case --
    see the dedicated positive test above for ``-n``; this file does not
    re-assert the old-style shape's own success here to avoid a
    contradictory parametrize entry)."""
    if name == "nice" and argv == ("nice", "-99", "ls"):
        # Old-style `-ADJUSTMENT` IS handled -- positive control, not a
        # refusal (kept in this list purely as documentation that this
        # shape was deliberately considered, not overlooked).
        assert extract_inner_argv(name, argv) == ("ls",)
        return
    assert extract_inner_argv(name, argv) is None


def test_unsupported_wrappers_table_names_sudo_and_find_exec_explicitly():
    """Tier 1: #6061's own architect ruling -- sudo/find -exec are NOT
    silently absent from this module; they are named, with why, in
    :data:`UNSUPPORTED_WRAPPERS`."""
    assert "sudo" in UNSUPPORTED_WRAPPERS
    assert "find" in UNSUPPORTED_WRAPPERS
    assert UNSUPPORTED_WRAPPERS["sudo"] and UNSUPPORTED_WRAPPERS["find"]


# ── Tier 2: check_exec_plan_policy, wired end to end ─────────────────────


def _ctx(tmp_path: Path, *, contextual: "ContextualPermission | None" = None):
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    collected = collect_events(events)
    workspace = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(config_permissions={}, project_root=project_root, interactive=False)
    ctx = OpContext(
        workspace=workspace, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test", contextual_permission=contextual,
    )
    return ctx, collected


@pytest.mark.asyncio
async def test_env_ls_is_denied_when_ls_itself_is_restricted(tmp_path: Path) -> None:
    """Tier 2: accept -- THE #6061 defect, closed. Before this PR, a tool
    axis restricting ``ls`` never saw ``ls`` at all in ``env ls`` (only
    ``env`` was checked). Strip-falsify (performed during review, file
    Edit only): reverting ``_check_segment`` to check only the outer
    ``resolved_name`` (never unwrapping) makes this assertion fail --
    ``env ls`` is silently approved."""
    ctx, collected = _ctx(tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"ls"})))
    plan = [ExecSegment(argv=("env", "ls"))]

    with pytest.raises(PermissionError, match="ls"):
        await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    await settle(ctx.events)
    (denied,) = [e for e in collected if e.type == "exec_tool_axis_denied"]
    assert denied.data["effective_name"] == "ls"
    assert denied.data["unwrap_chain"] == ["env", "ls"]


@pytest.mark.asyncio
async def test_env_itself_denied_still_stops_the_chain_at_env(tmp_path: Path) -> None:
    """Tier 2: accept -- an operator who denies the WRAPPER's own name
    (``env``) still has that respected; the chain-walk denies at ``env``
    before ever reaching ``ls`` (never silently unwrapped past a denial
    on the wrapper name itself)."""
    ctx, collected = _ctx(tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"env"})))
    plan = [ExecSegment(argv=("env", "ls"))]

    with pytest.raises(PermissionError, match="env"):
        await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    await settle(ctx.events)
    (denied,) = [e for e in collected if e.type == "exec_tool_axis_denied"]
    assert denied.data["effective_name"] == "env"
    assert denied.data["unwrap_chain"] == ["env"]


@pytest.mark.asyncio
async def test_deep_chain_denies_at_the_actual_binary_not_just_the_first_wrapper(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- a 2-layer chain (``env`` wrapping ``sh -c``
    wrapping the real binary) is walked to its end; the SAME operator
    restriction that would deny a bare ``rm`` also denies it 2 layers
    deep."""
    ctx, collected = _ctx(tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"rm"})))
    plan = [ExecSegment(argv=("env", "FOO=1", "sh", "-c", "rm"))]

    with pytest.raises(PermissionError, match="rm"):
        await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    await settle(ctx.events)
    (denied,) = [e for e in collected if e.type == "exec_tool_axis_denied"]
    assert denied.data["unwrap_chain"] == ["env", "sh", "rm"]


@pytest.mark.asyncio
async def test_env_ls_is_unaffected_when_no_restriction_is_configured(tmp_path: Path) -> None:
    """Tier 2: deny -- #6061's own "既定は変えません" ruling. No
    ``contextual_permission`` at all -- ``env ls`` (a wrapper chain that
    WOULD deny under the restriction above) passes cleanly, and the
    returned plan entry carries no ``unwrap_chain`` key at all (matching
    the byte-identical-shape contract ``test_5838_stage4_shc_exec.py``'s
    own exact-dict-equality test depends on)."""
    ctx, collected = _ctx(tmp_path, contextual=None)
    plan = [ExecSegment(argv=("env", "ls"))]

    result = await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    (entry,) = result
    assert "unwrap_chain" not in entry
    await settle(ctx.events)
    assert not [e for e in collected if e.type == "exec_tool_axis_denied"]


@pytest.mark.asyncio
async def test_wrapper_chain_never_walked_when_no_restriction_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: deny, the load-bearing structural witness for "既定は変え
    ません" -- with no tool-axis restriction, ``exec_wrappers.is_
    registered_wrapper`` (the FIRST call inside the unwrap loop) is never
    even invoked. A real, wrapping call recorder stands in for it (never
    a Mock -- delegates to the real function, only records that it ran),
    matching this repo's own "wrap and delegate, never fake" precedent
    for observing "was a seam reached" without a duration.

    Strip-falsify (performed during review, file Edit only): removing
    the ``if not restriction_active: ... return`` early-exit in
    ``_check_segment`` makes this assertion fail -- the wrapper table is
    consulted even for an unrestricted session."""
    import reyn.security.exec_plan_policy as policy_module

    calls: "list[str]" = []
    real = policy_module.is_registered_wrapper

    def _recording_is_registered_wrapper(name: str) -> bool:
        calls.append(name)
        return real(name)

    monkeypatch.setattr(policy_module, "is_registered_wrapper", _recording_is_registered_wrapper)

    ctx, _ = _ctx(tmp_path, contextual=None)
    plan = [ExecSegment(argv=("env", "ls"))]

    await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    assert calls == [], (
        f"expected the wrapper table never consulted when unrestricted, got {calls!r}"
    )


@pytest.mark.asyncio
async def test_a_form_this_table_cannot_extract_is_denied_when_restricted(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- a registered wrapper (``env``) in a flag shape
    ``exec_wrappers.py`` refuses to extract (``-u`` takes a separate
    value) is denied outright once a restriction is active, rather than
    silently treated as "nothing more to check"."""
    ctx, collected = _ctx(tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"nonexistent"})))
    plan = [ExecSegment(argv=("env", "-u", "PATH", "ls"))]

    with pytest.raises(PermissionError, match="cannot verify"):
        await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    await settle(ctx.events)
    (denied,) = [e for e in collected if e.type == "exec_tool_axis_denied"]
    assert denied.data["effective_name"] == "env"


@pytest.mark.asyncio
async def test_a_chain_deeper_than_the_unwrap_limit_is_denied(tmp_path: Path) -> None:
    """Tier 2: accept -- 9 nested ``env`` hops (one past the 8-hop limit,
    matching OpenAI Codex's own depth bound) is refused rather than
    approved, even though every individual hop is itself extractable."""
    ctx, collected = _ctx(tmp_path, contextual=ContextualPermission(tool_deny=frozenset({"nonexistent"})))
    argv = ("env",) * 9 + ("ls",)
    plan = [ExecSegment(argv=argv)]

    with pytest.raises(PermissionError, match="depth limit|exceeded"):
        await check_exec_plan_policy(plan, ctx, env_path=_REAL_PATH, cwd=None)

    await settle(ctx.events)
    (denied,) = [e for e in collected if e.type == "exec_tool_axis_denied"]
    # Content, not shape: every hop walked was genuinely "env" (the SAME
    # binary all 9 argv tokens name), and the chain's own last entry is
    # exactly the one that tripped the limit -- not merely "some length".
    assert denied.data["unwrap_chain"] == ["env"] * (_MAX_UNWRAP_DEPTH + 1)
    assert "unwrap limit" in denied.data["reason"] and str(_MAX_UNWRAP_DEPTH) in denied.data["reason"]
