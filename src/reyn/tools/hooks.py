"""Hooks ToolDefinitions — #2073 S3 (the LLM-op self-reload trigger).

``hooks_add`` — the agent adds a push hook at an agent-lifecycle point, written to
the RUNTIME hooks layer (``.reyn/config/hooks.yaml``) and applied at the next turn boundary
via the S2b hooks reapply seam. The crown-jewel of config hot-reload: the agent
expands its own hooks (autonomous capability-expansion), bounded by the safety
trifecta + the existing hook safeguards:

- **Write-gate by construction**: the tool writes ONLY the hardcoded
  ``.reyn/config/hooks.yaml`` (the IN-set runtime layer). It takes the hook CONTENT, never a
  path, so it is *structurally impossible* to aim at ``reyn.yaml`` (the restart-only
  OUT-set: security / budget / the loop valve).
- **validate-before-apply** (S2b) rejects a malformed reload; **boot-resilience**
  (S2b) degrades a malformed persisted layer at the next boot. Plus write-time
  validation here (a bad hook → an op error, not a silent bad write).
- **Permission** is the TOOL axis: the calling agent must list ``hooks_add`` in
  ``permissions.tool`` (``require_tool``) and the #2074 capability profile
  (``tool_deny``) can deny self-reload. The damage is bounded — F is sandboxed, E is
  loop-valved (``safety.loop.max_hook_driven_turns``), C is benign.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from reyn.tools.descriptions import hooks as _hooks_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_HOOK_POINTS = [
    "turn_start", "turn_end", "session_start", "session_end",
    "task_start", "task_end",
]
# Isolation note (#2898): the schema below embeds ``list(_HOOK_POINTS)`` — a
# defensive copy — NOT the module list by reference. ``render_for_router`` only
# shallow-copies ``parameters``, so a by-reference embed would alias the module
# list into every rendered schema; any later mutation of ``_HOOK_POINTS`` would
# then silently corrupt every ``hooks_add`` render for the rest of the process
# (a shared-mutable-state × test-order flake vector). The copy decouples the
# rendered enum from the module list (same convention as
# ``universal_catalog.py``'s ``"enum": list(CATEGORIES)``).

# Relocated to reyn.tools.descriptions.hooks (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_HOOKS_ADD_DESCRIPTION = _hooks_descriptions.hooks_add.text

# proposal 0060 D5d: the same doc this description already hand-points at
# (descriptions/hooks.py) — generalized here as the first-class structured
# field (the exemplar the reachability audit (D2) singled out as the ONE
# REACHABLE part-type; other ops get this same field, not a hand-written
# sentence, going forward).
_HOOKS_ADD_DOC_REF = "docs/concepts/runtime/hooks.md"

_HOOKS_ADD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "on": {
            "type": "string", "enum": list(_HOOK_POINTS),
            "description": _hooks_descriptions.PARAMS["hooks_add"]["on"].text,
        },
        "message": {
            "type": "string",
            "description": _hooks_descriptions.PARAMS["hooks_add"]["message"].text,
        },
        "wake": {
            "type": "boolean",
            "description": _hooks_descriptions.PARAMS["hooks_add"]["wake"].text,
        },
        "push_when": {
            "type": "string",
            "description": _hooks_descriptions.PARAMS["hooks_add"]["push_when"].text,
        },
        "name": {
            "type": "string",
            "description": _hooks_descriptions.PARAMS["hooks_add"]["name"].text,
        },
    },
    "required": ["on", "message"],
}


# ── Storage helpers (= .reyn/config/hooks.yaml read/write) ─────────────────────────


def _hooks_yaml_path(ctx: ToolContext) -> Path:
    """The canonical ``.reyn/config/hooks.yaml`` path under the project root (HARDCODED —
    the write target is never derived from LLM input)."""
    root = getattr(ctx.workspace, "root", None) or getattr(ctx.workspace, "base_dir", None)
    if root is None:
        root = Path.cwd()
    return Path(root) / ".reyn" / "config" / "hooks.yaml"


def _normalize_on(h: object) -> object:
    """Normalize the YAML-1.1 quirk where a bare ``on:`` key parses as boolean
    ``True`` — map ``{True: pt}`` → ``{"on": pt}`` so dedup compares like-for-like."""
    if isinstance(h, dict) and True in h and "on" not in h:
        h = dict(h)
        h["on"] = h.pop(True)
    return h


def _read_hooks(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_hooks(path: Path, data: dict) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _hooks_list(data: dict) -> list:
    hooks = data.get("hooks", [])
    if not isinstance(hooks, list):
        return []
    return [_normalize_on(h) for h in hooks]


async def _gate(ctx: ToolContext) -> None:
    """Permission gate: ``require_file_write`` against the canonical .reyn/config/hooks.yaml.
    TOOL-level authorisation already happened at agent startup (``require_tool``
    against the agent's ``permissions.tool``) + the #2074 capability profile. No-op
    in unit-test contexts (``ctx.permission_resolver`` is None)."""
    from reyn.security.permissions.permissions import PermissionDecl
    if ctx.permission_resolver is None:
        return
    hooks_yaml_path = str(_hooks_yaml_path(ctx))
    decl = PermissionDecl(file_write=[{"path": hooks_yaml_path, "scope": "just_path"}])
    ctx.permission_resolver.session_approve_path(hooks_yaml_path, "hooks", "file.write")
    # bus= not threaded: the session_approve_path above pre-approves this exact
    # path (AgentLayer._approved → True) and no sandbox_policy is passed
    # (SandboxLayer ⊤, no veto), so require_file_write's EffectivePermission
    # returns early — the JIT-ask branch (`if bus is not None`) is unreachable
    # here. Exempt, not an oversight (#3089 registry audit, 2 of 2 exempt sites).
    await ctx.permission_resolver.require_file_write(decl, hooks_yaml_path, "hooks")


# ── Handler ─────────────────────────────────────────────────────────────────


async def _handle_hooks_add(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Add a push hook to the runtime layer (.reyn/config/hooks.yaml) + schedule a reload."""
    on = str(args["on"])
    message = str(args["message"])
    wake = bool(args.get("wake", True))
    push_when = args.get("push_when")
    name = args.get("name")

    template_push: dict = {"message": message, "wake": wake}
    if push_when is not None:
        template_push["push_when"] = str(push_when)
    hook: dict = {"on": on, "template_push": template_push}
    if name:
        hook["name"] = str(name)

    # Write-time validate (defense-in-depth; the reload's validate-before-apply also
    # guards, and a persisted-malformed layer is handled by boot-resilience).
    from reyn.hooks import HookConfigError, load_hooks
    try:
        load_hooks([hook])
    except HookConfigError as exc:
        return {"status": "error", "error": f"invalid hook: {exc}"}

    await _gate(ctx)

    # Persist to the FIXED .reyn/config/hooks.yaml (structurally cannot target reyn.yaml).
    path = _hooks_yaml_path(ctx)
    data = _read_hooks(path)
    hooks = _hooks_list(data)
    added = hook not in hooks  # dedup exact duplicates (idempotent re-add)
    if added:
        hooks.append(hook)
    data["hooks"] = hooks
    _write_hooks(path, data)

    # #2259 PR-1: record the FULL post-mutation hooks registry as a truncation-surviving
    # config generation so it recovers (the yaml is a derived projection). The helper guards
    # internally — no-op when there is no WAL or the path is outside the project `.reyn`.
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), path, data)

    # Schedule the reload — the HotReloader applies the S2b hooks seam at the turn
    # boundary (1 turn = 1 config snapshot; never mid-turn).
    # Per-session route (#2073 S3): reload THIS calling session's reloader (multi-agent
    # correctness — the reloader is per-session, so a process-wide global would reload
    # the wrong session). Fall back to the active reloader for non-session/test contexts.
    from reyn.runtime.hot_reload import get_active_hot_reloader
    reloader = getattr(ctx, "hot_reloader", None) or get_active_hot_reloader()
    scheduled = reloader is not None
    if scheduled:
        reloader.request_reload(source="llm_op")

    return {
        "status": "ok",
        "on": on,
        "added": added,
        "reload_scheduled": scheduled,
        "path": str(path),
    }


from reyn.core.offload.canonical import hooks_add_to_canonical  # noqa: E402

HOOKS_ADD = ToolDefinition(
    canonical=hooks_add_to_canonical,
    name="hooks_add",
    description=_HOOKS_ADD_DESCRIPTION,
    parameters=_HOOKS_ADD_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_hooks_add,
    category="hooks",
    purity="side_effect",
    doc_ref=_HOOKS_ADD_DOC_REF,
)
