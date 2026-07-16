"""presentation_install kind handler — register a named presentation template into
the project config (proposal 0060 Phase 1 Layer A, A8).

Handler logic (one-shot, no sub-phases, no source/git-fetch path — a blueprint is
small declarative data carried inline, never a file-backed artifact):

  1. Structural threat gate: :func:`reyn.core.present.catalog.validate_blueprint`
     on ``op.blueprint``. This IS the threat gate for this op (no
     ``scan_for_threats`` call — there is no free-text field to scan). A present
     blueprint is structurally non-executable by construction (8 fixed
     components, every non-literal value is a ``$bind`` RFC-6901 JSON-Pointer, no
     template-ref / eval / exec surface, ``image.src`` renders as a label — no
     fetch/SSRF) — the SAME gate an inline ``present(blueprint=...)`` op already
     passes through. A malformed / non-catalog blueprint is refused BEFORE any
     config mutation.
  2. Gate via ``PermissionResolver.require_file_write`` for the presentations.yaml
     path.
  3. Read ``.reyn/config/presentations.yaml`` (or empty dict), set
     ``presentations.entries.<name>`` = ``{blueprint, enabled, provenance}``,
     write back. ``provenance`` is stamped from ``ctx.turn_origin`` alone (A9,
     A7) — the op schema carries no provenance field for the LLM to supply.
  4. ``record_config_generation`` on the presentations.yaml path AFTER write —
     inherits the existing config crash-recovery (no new recovery-gated
     obligation; mirrors skill_install/pipeline_install).
  5. Emit ``presentation_installed`` event (P6 audit trail).
  6. Reload so the installed template goes live: a PURE ADDITION on a live
     per-session reloader (``ctx.hot_reloader``) applies IMMEDIATELY (mid-turn)
     via the "presentations" seam (the SAME seam FP-0054 PR-C registers for
     operator edits to presentations.yaml); a same-name overwrite or no
     per-session reloader keeps the deferred turn-boundary path.

Ships inert-by-construction (A3, no new state needed): a present-view is
invoke-by-name — it renders only when a ``present(view=<name>)`` op names it, so
a freshly-installed template is discoverable but dormant until referenced,
exactly like a builtin skill (``visibility="on_demand"``) or pipeline
(invoke-by-name).

This mirrors ``skill_install.py`` / ``pipeline_install.py``'s STRUCTURE
(permission gate → config write → record_config_generation → emit event →
hot-reload) but the threat is LOWER than either (no source/git-fetch surface,
no free-text description, ``validate_blueprint`` already fills the
``scan_for_threats`` role).
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.present import PresentBlueprintError, validate_blueprint
from reyn.schemas.models import PresentationInstallIROp

from . import register
from .context import OpContext
from .context import provenance_from_ctx as _provenance_from_ctx
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx
from .skill_install import _read_yaml, _resolve_project_root, _write_yaml

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _presentations_config_path(project_root: Path) -> Path:
    """Canonical path for the dynamic presentations registry config."""
    return project_root / ".reyn" / "config" / "presentations.yaml"


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(
    op: PresentationInstallIROp,
    ctx: OpContext,
) -> dict:
    """Execute a presentation_install op — register a named presentation template.

    Validates ``op.blueprint`` structurally (the threat gate, A8), gates the
    config write, persists the entry with an OS-stamped ``provenance`` (A9),
    records a config generation for crash-recovery, emits an audit event, and
    requests a hot-reload of the "presentations" seam.
    """
    project_root = _resolve_project_root(ctx.workspace)
    name = (op.name or "").strip()
    if not name:
        return {
            "kind": "presentation_install",
            "status": "error",
            "error": "name must be a non-empty string (the presentations.entries key).",
        }

    # ── 1. Structural threat gate: validate_blueprint (A8) ────────────────────
    # This IS the threat gate for present-install — no free-text field, so no
    # scan_for_threats call. Refuses BEFORE any config mutation.
    try:
        validate_blueprint(op.blueprint)
    except PresentBlueprintError as exc:
        ctx.events.emit(
            "presentation_install_blocked",
            name=name,
            error=str(exc),
        )
        return {
            "kind": "presentation_install",
            "status": "blocked",
            "name": name,
            "error": (
                f"install blocked: blueprint failed the structural gate: {exc}. "
                "A presentation blueprint must use only the catalog components "
                "(text/markdown/code/diff/keyvalue/table/list/image) with "
                "$bind JSON-Pointer values."
            ),
        }

    # ── 2. Permission gate: presentations.yaml write ──────────────────────────
    config_path = _presentations_config_path(project_root)
    if ctx.permission_resolver is not None:
        _sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=_sandbox,
        )

    # ── 3. Write presentations.entries.<name> to .reyn/config/presentations.yaml ─
    existing = _read_yaml(config_path)
    if "presentations" not in existing or not isinstance(existing.get("presentations"), dict):
        existing["presentations"] = {}
    if (
        "entries" not in existing["presentations"]
        or not isinstance(existing["presentations"].get("entries"), dict)
    ):
        existing["presentations"]["entries"] = {}
    entry: dict = {
        "blueprint": op.blueprint,
        "enabled": True,
    }
    # proposal 0060 Phase 1 Layer A (A9): provenance is stamped from the single
    # OS-authoritative source (ctx.turn_origin, set by
    # Session._stamp_execution_context — A7) — never from an op field, so an
    # auto-improvement turn cannot self-declare "user_directed" to bypass the
    # Phase-4 gate. The `builtin` value is stamped on a DIFFERENT seam (the
    # future builtin-tier registry-build loader, not this install path) — never
    # written here. LOAD-BEARING fail-safe: provenance_from_ctx collapses an
    # unset ctx.turn_origin (a bridge-fallback path that didn't thread it) to the
    # stricter "auto_improvement" — a provenance=None install would be UNGATED
    # (escape the Phase-4 gate), so this closes that gate-bypass by construction.
    entry["provenance"] = _provenance_from_ctx(ctx)
    # #2761-style PR-2 mirror: capture pure-addition-vs-overwrite BEFORE the
    # write mutates entries, so step 6 routes a NEW name to the immediate
    # mid-turn apply and a same-name overwrite (clobber-update — presentation's
    # only update path) to the deferred path.
    from reyn.runtime.hot_reload import is_pure_addition  # noqa: PLC0415
    _is_addition = is_pure_addition(name, existing["presentations"]["entries"])
    existing["presentations"]["entries"][name] = entry
    _write_yaml(config_path, existing)

    # ── 4. Record config generation for crash-recovery (#2259 / CLAUDE.md gate) ─
    # No new recovery-gated obligation (A8): this inherits the EXISTING config
    # crash-recovery record_config_generation already provides — no
    # truncate-falsify test owed for this op (config-generation recovery is
    # already covered where record_config_generation is exercised).
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), config_path, existing)

    # ── 5. Emit presentation_installed event (P6) ─────────────────────────────
    ctx.events.emit(
        "presentation_installed",
        name=name,
        config_path=str(config_path),
    )

    # ── 6. Hot-reload: surface the installed template in the current session ──
    from reyn.runtime.hot_reload import dispatch_install_reload  # noqa: PLC0415
    await dispatch_install_reload(
        getattr(ctx, "hot_reloader", None),
        source="presentation_install",
        is_addition=_is_addition,
    )

    return {
        "status": "installed",
        "name": name,
        "config_path": str(config_path),
    }


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("presentation_install", handle, canonical=STRUCTURED_PASSTHROUGH)
