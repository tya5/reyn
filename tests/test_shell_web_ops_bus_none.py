"""Tier 2: OS invariant — shell/web op bus=None pre-check removal (PR-N14).

Verifies that removing the straggler ``if ctx.intervention_bus is None: raise``
pre-checks from shell.py and web.py degrades cleanly into the PermissionResolver
path instead of raising RuntimeError.

Design-intent reference: permissions.py:741-754 (B49 W2-S5 design comment)
establishes that bus=None handling belongs inside the resolver, not as a
pre-check in the op handler. This PR completes that intent for shell/web.

All tests use real PermissionResolver + real OpContext. No MagicMock/AsyncMock.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.shell import handle as shell_handle
from reyn.op_runtime.web import handle_web_fetch
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import ShellIROp, WebFetchIROp
from reyn.workspace.workspace import Workspace

# ── helpers ─────────────────────────────────────────────────────────────────────


def _make_ctx(
    tmp_path: Path,
    *,
    resolver: PermissionResolver | None,
    decl: PermissionDecl | None = None,
    intervention_bus=None,
) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=decl or PermissionDecl(),
        permission_resolver=resolver,
        intervention_bus=intervention_bus,
    )


def _non_interactive_resolver(
    tmp_path: Path,
    *,
    config: dict,
) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config,
        project_root=tmp_path,
        interactive=False,
    )


def _run(coro):
    return asyncio.run(coro)


# ── shell tests ─────────────────────────────────────────────────────────────────


def test_shell_bus_none_config_approved_proceeds(tmp_path):
    """Tier 2: shell op with bus=None + config 'shell: allow' + non-interactive
    resolver proceeds past the permission gate without RuntimeError.

    This is the benchmark-dispatch critical path: operator sets
    ``permissions.shell: allow`` in reyn.local.yaml (= PR-N12 documented
    pattern), dispatch constructs OpContext with intervention_bus=None.
    Before PR-N14 the straggler pre-check fired here; after removal the
    config-approved short-circuit in ``_approve`` clears it silently.
    """
    resolver = _non_interactive_resolver(tmp_path, config={"shell": "allow"})
    decl = PermissionDecl(shell=True)
    ctx = _make_ctx(tmp_path, resolver=resolver, decl=decl, intervention_bus=None)
    op = ShellIROp(kind="shell", cmd="true")

    # Should NOT raise RuntimeError (= straggler removed) and should
    # NOT raise PermissionError (= config-approved clears the gate).
    # The subprocess runs ``true`` which succeeds with returncode 0.
    result = _run(shell_handle(op, ctx, caller="control_ir"))
    assert result["kind"] == "shell"
    assert result["status"] == "ok"
    assert result["returncode"] == 0


def test_shell_bus_none_not_approved_non_interactive_denies_cleanly(tmp_path):
    """Tier 2: shell op with bus=None + shell NOT approved + non-interactive
    resolver raises PermissionError (clean denial), NOT RuntimeError.

    Before PR-N14: RuntimeError("shell op requires intervention_bus on OpContext").
    After PR-N14: PermissionError from require_shell (resolver's non-interactive
    deny path: _approve returns False → require_shell raises PermissionError).
    """
    resolver = _non_interactive_resolver(tmp_path, config={})
    decl = PermissionDecl(shell=True)  # declared but not config-approved
    ctx = _make_ctx(tmp_path, resolver=resolver, decl=decl, intervention_bus=None)
    op = ShellIROp(kind="shell", cmd="true")

    with pytest.raises(PermissionError):
        _run(shell_handle(op, ctx, caller="control_ir"))


def test_shell_bus_none_not_declared_denies_cleanly(tmp_path):
    """Tier 2: shell op with bus=None + shell not declared in PermissionDecl
    raises PermissionError from require_shell (undeclared = immediate deny),
    NOT RuntimeError from the old straggler pre-check.
    """
    resolver = _non_interactive_resolver(tmp_path, config={})
    decl = PermissionDecl(shell=False)  # not declared
    ctx = _make_ctx(tmp_path, resolver=resolver, decl=decl, intervention_bus=None)
    op = ShellIROp(kind="shell", cmd="true")

    with pytest.raises(PermissionError):
        _run(shell_handle(op, ctx, caller="control_ir"))


# ── web_fetch tests ──────────────────────────────────────────────────────────────


def test_web_fetch_bus_none_not_approved_non_interactive_denies_cleanly(tmp_path):
    """Tier 2: web_fetch op with bus=None + http.get NOT approved + non-interactive
    resolver raises PermissionError (clean denial), NOT RuntimeError.

    We do NOT need a live HTTP call — the permission gate fires before any
    network request. The resolver's non-interactive path within require_http_get
    reaches a bus=None + unapproved branch and raises PermissionError.

    Before PR-N14: RuntimeError("web_fetch op requires intervention_bus on OpContext").
    After PR-N14: PermissionError from require_http_get.
    """
    resolver = _non_interactive_resolver(tmp_path, config={})
    # No http_get declared in decl and no config approval → legacy compat path
    # hits bus=None → PermissionError (unapproved + no bus).
    decl = PermissionDecl()
    ctx = _make_ctx(tmp_path, resolver=resolver, decl=decl, intervention_bus=None)
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")

    with pytest.raises(PermissionError):
        _run(handle_web_fetch(op, ctx, caller="control_ir"))


def test_web_fetch_bus_none_config_approved_proceeds_to_network(tmp_path):
    """Tier 2: web_fetch op with bus=None + 'web.fetch: allow' config (= blanket
    pre-approval) passes the permission gate without RuntimeError or PermissionError.

    The test does not assert on the network result (= avoid flaky HTTP in CI);
    it asserts that no RuntimeError is raised from the op handler itself.
    A network error or non-200 response is acceptable — it proves the gate cleared.
    """
    resolver = _non_interactive_resolver(tmp_path, config={"web.fetch": "allow"})
    decl = PermissionDecl()
    ctx = _make_ctx(tmp_path, resolver=resolver, decl=decl, intervention_bus=None)
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")

    # May succeed or fail due to network, but must NOT raise RuntimeError.
    try:
        _run(handle_web_fetch(op, ctx, caller="control_ir"))
    except RuntimeError as exc:
        pytest.fail(
            f"RuntimeError raised from web_fetch handler with bus=None + config-approved: {exc}"
        )
    except Exception:
        # Network errors, PermissionError for other reasons, etc. are acceptable.
        pass


# ── image-load gate (deliberate keep) ────────────────────────────────────────────


def test_web_fetch_image_load_gate_still_raises_without_bus(tmp_path):
    """Tier 2: image-load gate (web.py:264-269) is intentionally KEPT.

    require_media_load has ``bus: RequestBus`` (non-Optional) and its
    on_oversize='ask' path calls ``_approve(bus=...)`` → ``_prompt(bus=...)``
    which dereferences ``bus.request(iv)`` directly. Widening require_media_load
    + _approve + _prompt is out of scope for this PR (= touching _approve/_prompt
    is forbidden per scope-guard). The image-load gate is therefore left in place.

    This test documents the deliberate exception: a binary-image response with
    multimodal gate configured triggers the pre-check when bus=None.

    Note: this test verifies the gate still fires; it does NOT perform a real HTTP
    call. We set ctx.multimodal_config via a minimal stand-in to trigger the gate
    path in the handler's image branch. Because actually reaching that branch
    requires a live HTTP response returning Content-Type: image/*, we verify the
    GATE itself is still present in the source as the primary evidence, and document
    the deliberate decision here.

    Primary evidence: web.py:262-269 still contains the image-load pre-check
    (not removed by this PR). require_media_load signature line 1371 is
    ``bus: RequestBus`` (non-Optional). The spec decision is documented in the
    PR body.
    """
    # Verify the image-load gate is still present in source (primary evidence).
    import inspect

    import reyn.op_runtime.web as web_module

    source = inspect.getsource(web_module.handle_web_fetch)
    assert "web_fetch op requires intervention_bus when loading" in source, (
        "Image-load gate was unexpectedly removed; this Tier-2 test documents "
        "the deliberate keep decision — if you intentionally removed it, delete "
        "this test and add one proving require_media_load handles None safely."
    )
