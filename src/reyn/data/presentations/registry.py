"""reyn.data.presentations.registry — config-entries → PresentationRegistry loader (FP-0054 PR-C).

The production population path for named presentation templates (the FP-0054 §3
4-stage fallback's stage 1 — "registered template, operator-owned"). Templates
are registered PURELY via explicit ``presentations.entries`` declarations in
config — the same registration model as
``reyn.data.pipelines.registry.build_pipeline_registry`` /
``reyn.data.skills.registry.build_skill_registry`` / ``mcp.servers``.

Unlike a skill (metadata only, the file is never parsed) and like a pipeline (the
value must be PARSED/validated to be usable), a named template's value is a
**blueprint** — the same declarative component tree an inline blueprint is —
carried INLINE in the entry (a blueprint is small declarative data, not a
file-backed artifact like a pipeline DSL or a ``SKILL.md``, so it needs no path
indirection). Each entry's ``blueprint`` is validated through
:func:`reyn.core.present.catalog.validate_blueprint` at build time, and the
registry stores the NORMALIZED node list keyed by the entry name. So a registered
template is already-valid nodes at render time — a malformed template is rejected
at config load, never at ``present`` dispatch.

**Operator/config action, not an LLM action.** There is deliberately NO install
op: registering a named template is a write-gate operator action (the operator
edits ``presentations.yaml``); the LLM authors inline blueprints only. This is why
this loader has no ``op.name`` vs declared-name reconciliation (a blueprint has no
self-declared name — the entry key IS the name) and no
``request_reload``-from-an-op path.

Failure posture is PER-ENTRY ISOLATED, not process-fatal (mirrors
``build_pipeline_registry``): a malformed blueprint or a non-dict entry is caught
INSIDE the ``entries`` loop — logged as a ``logging`` warning AND durably emitted
as a ``presentation_load_failed`` P6 event (via ``emit_cli_event`` — this loader
has no per-session ``ctx``/``EventLog`` handle and is called from multiple
entrypoints, so the session-independent sink is the right fit), then SKIPPED — the
remaining entries still load. This matters because
``SessionFactoryConfig.from_config`` calls this at EVERY session construction with
no enclosing try/except, so one broken template entry must not crash reyn startup.

``strict=True`` (opt-in, default ``False``) restores the fail-loud posture — the
first per-entry failure raises :class:`PresentationLoadError` straight out of
:func:`build_presentation_registry` with NO logging/eventing. This exists for the
``presentations`` hot-reload seam (``Session._reapply_presentations``): that seam
needs ATOMIC last-good-registry semantics — reject the ENTIRE rebuild on any
broken entry and keep serving the old registry unchanged, rather than silently
dropping just the broken template from a live session mid-reload. Session-factory
construction is the opposite case (a NEW session has no "old registry" to fall
back to), so ``strict=False`` is right there: isolate the bad entry, keep the good
ones, let the session start.

``raw_presentations=None`` (the util/no-root path), an empty/absent
``presentations:`` block, or an empty ``entries`` map → an empty registry (zero
templates is a valid, non-error state — same as skills/pipelines). These return
before the per-entry loop even starts, so nothing is logged.
"""
from __future__ import annotations

import logging
from typing import Any

from reyn.core.present import PresentBlueprintError, validate_blueprint

logger = logging.getLogger(__name__)


class PresentationLoadError(ValueError):
    """A ``presentations.entries`` declaration could not be loaded (missing /
    malformed ``blueprint``, or a blueprint that fails the structural
    :func:`validate_blueprint` gate). Raised internally by :func:`_load_one_entry`
    and caught PER-ENTRY by :func:`build_presentation_registry` (logged + durably
    emitted as a ``presentation_load_failed`` event, then skipped — see the module
    docstring), unless ``strict=True``."""


class PresentationRegistry:
    """An in-memory ``name -> validated blueprint nodes`` map for named templates.

    A template's value is the NORMALIZED node list returned by
    :func:`validate_blueprint` (structure only; leaf neutralization happens later
    at the render seam, exactly as for an inline blueprint). Built fresh + swapped
    on hot-reload (never mutated in place), so the reference the op resolves
    against is always a consistent snapshot.
    """

    def __init__(self) -> None:
        self._templates: dict[str, list[dict]] = {}

    def register(self, name: str, nodes: list[dict]) -> None:
        """Register a validated template under ``name``. Re-registration of an
        already-present name raises :class:`ValueError` (the caller resolves
        first-registered-wins by catching this — mirrors ``PipelineRegistry``)."""
        if name in self._templates:
            raise ValueError(f"presentation template {name!r} is already registered")
        self._templates[name] = nodes

    def get(self, name: str) -> "list[dict] | None":
        """The validated node list for ``name``, or ``None`` when unregistered
        (an unknown name is NOT an error — it falls through the present op's
        fallback chain to the generic viewer)."""
        return self._templates.get(name)

    def has(self, name: str) -> bool:
        """True iff ``name`` is a registered template."""
        return name in self._templates

    def names(self) -> list[str]:
        """The registered template names (declaration order)."""
        return list(self._templates)


def _load_one_entry(key: str, raw: Any, registry: PresentationRegistry) -> None:
    """Load + register ONE ``presentations.entries.<key>`` declaration.

    Raises :class:`PresentationLoadError` for a missing ``blueprint``, a blueprint
    that fails the structural gate, or a duplicate name across entries. This
    function fails loud; :func:`build_presentation_registry` is the isolation
    boundary that catches it per-entry.
    """
    if not isinstance(raw, dict):
        raise PresentationLoadError(
            f"presentations.entries.{key!r} must be a mapping, got {type(raw).__name__}"
        )
    if "blueprint" not in raw:
        raise PresentationLoadError(
            f"presentations.entries.{key!r} has no 'blueprint' — a named template's "
            "value is a declarative component tree (the same shape as an inline "
            "blueprint)."
        )
    try:
        nodes = validate_blueprint(raw["blueprint"])
    except PresentBlueprintError as exc:
        raise PresentationLoadError(
            f"presentations.entries.{key!r}: blueprint failed the structural gate: {exc}"
        ) from exc

    try:
        registry.register(key, nodes)
    except ValueError as exc:
        # Duplicate name — first-registered wins; this (later) entry fails.
        raise PresentationLoadError(
            f"presentations.entries.{key!r}: {exc}"
        ) from exc


def build_presentation_registry(
    raw_presentations: "dict[str, Any] | None", *, strict: bool = False,
) -> PresentationRegistry:
    """Build a populated :class:`PresentationRegistry` from the ``presentations:``
    config dict.

    For each ``presentations.entries.<key>`` declaration, the inline ``blueprint``
    is validated via :func:`validate_blueprint` and registered under the entry key
    (the authoritative name a ``present`` op's ``view`` resolves against;
    FP-0055 PR-1 renamed the op arg from ``template`` — the registry's own
    vocabulary, "named templates", is unchanged prose here, only the op-arg
    reference is updated).

    ``raw_presentations=None`` / an empty-or-absent ``presentations:`` block / a
    block with no ``entries`` → an empty registry (zero-config default, not a
    failure — nothing logged). Otherwise each per-entry failure (missing / malformed
    blueprint, duplicate name) is caught PER ENTRY, logged, durably emitted as a
    ``presentation_load_failed`` P6 event, then skipped — unless ``strict=True``,
    which re-raises the first failure (the hot-reload seam's atomic posture; see the
    module docstring).
    """
    from reyn.core.events.events import emit_cli_event

    registry = PresentationRegistry()
    if not isinstance(raw_presentations, dict):
        return registry

    raw_entries = raw_presentations.get("entries")
    if not isinstance(raw_entries, dict):
        return registry

    for key, raw in raw_entries.items():
        key = str(key)
        if isinstance(raw, dict) and not bool(raw.get("enabled", True)):
            continue

        try:
            _load_one_entry(key, raw, registry)
        except PresentationLoadError as exc:
            if strict:
                raise
            logger.warning(
                "presentations.entries.%r failed to load and was skipped: %s",
                key, exc,
            )
            try:
                emit_cli_event(
                    "presentation_load_failed",
                    key=key,
                    error=str(exc),
                )
            except Exception:  # noqa: BLE001 -- durable-capture must never crash startup
                pass
            continue

    return registry


__all__ = ["PresentationRegistry", "build_presentation_registry", "PresentationLoadError"]
