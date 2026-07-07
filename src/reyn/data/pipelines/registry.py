"""reyn.data.pipelines.registry — config-entries → PipelineRegistry loader.

The production population path for the pipeline feature. Pipelines are
registered PURELY via explicit ``pipelines.entries`` declarations in config —
the same registration model as ``reyn.data.skills.registry.build_skill_registry``
and ``mcp.servers`` (clean break: the prior directory-scan model — a blind
``scan_dirs`` glob over a ``pipelines/`` directory for any ``*.yaml`` file
present — is removed; a DSL file with no config entry is invisible to every
session).

Unlike skills (explicit ``entries`` config, metadata only, never parses the
file), a pipeline must be PARSED to be usable — the ``PipelineRegistry`` stores
live ``Pipeline`` objects and surfaces their name+description to the LLM (IS-5's
D19 catalog enumerator). So this loader parses each entry's ``path`` via
:func:`~reyn.core.pipeline.parser.parse_pipeline_dsl` and registers the result
under the pipeline's OWN declared ``pipeline:`` name (``Pipeline.name``) —
authoritative because that is the identity a ``call``/``match`` step's
``pipeline: LIT`` resolves against, NOT the config entry's key. A config entry
key that disagrees with its file's declared name is a footgun (the key you'd
naturally look for is not the key a `call` step actually resolves against) —
this loader refuses that mismatch (same posture as the install op's op.name vs
declared-name check in ``op_runtime/pipeline_install.py``).

Failure posture is PER-ENTRY ISOLATED, not process-fatal: a malformed DSL file,
an unreadable path, a config-key / declared-name mismatch, or a duplicate
declared name across entries are each caught INSIDE the ``entries`` loop —
logged as a Python ``logging`` warning AND durably emitted as a
``pipeline_load_failed`` P6 event (via ``emit_cli_event`` — see its own
docstring; this loader has no per-session ``ctx``/``EventLog`` handle threaded
in, and is called from multiple distinct entrypoints, so the session-independent
sink is the right fit), then SKIPPED — the remaining entries still load. This
was changed from an earlier fail-loud design (raise straight out of
``build_pipeline_registry``) because ``SessionFactoryConfig.from_config``
(``reyn/runtime/factory_config.py``) calls this with no enclosing try/except,
at EVERY session construction — so one broken pipeline entry anywhere in
``pipelines.entries`` crashed reyn's ENTIRE startup (`reyn chat` / `reyn web`),
not just that one pipeline. Visibility is still preserved (the warning + event
carry the exact same descriptive message the old raise would have) — this is
NOT the fully-silent ``skills.registry`` pattern (a typo must not silently
vanish with zero trace); it is "visible but non-fatal".

Duplicate declared name across entries: the FIRST-registered entry with that
name wins; a later entry that collides is the one skipped/logged (dict
iteration order = config declaration order — the first entry an operator
listed keeps its registration).

``raw_pipelines=None`` (the util/no-root path) → an empty registry; an
empty/absent ``pipelines:`` block or an empty ``entries`` map → an empty
registry (zero pipelines is a valid, non-error state — same as skills). These
are the only cases where ``build_pipeline_registry`` returns early WITHOUT
even entering the per-entry loop — they are not "failures", just an absent
config shape, so nothing is logged.

``strict=True`` (opt-in, default ``False``) restores the ORIGINAL fail-loud
posture — the first per-entry failure raises :class:`PipelineLoadError`
straight out of ``build_pipeline_registry``, with NO logging/eventing (the
raise itself is the signal). This exists for ``Session._reapply_pipelines``
(``reyn/runtime/session.py``, the ``pipelines`` hot-reload seam): that seam
needs ATOMIC last-good-registry semantics — reject the ENTIRE rebuild on any
broken entry and keep serving the old registry unchanged, rather than
silently dropping just the broken pipeline from a live session's registry
graph mid-reload (a "the pipeline this operator was relying on just vanished
from the running session" surprise). Session-factory construction
(``SessionFactoryConfig.from_config``) is the opposite case — a NEW session
about to start has no "old registry" to fall back to, so ``strict=False``
(the default) is the right posture there: isolate the bad entry, keep the
good ones, let the session start.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from reyn.core.pipeline.registry import PipelineRegistry

logger = logging.getLogger(__name__)


class PipelineLoadError(ValueError):
    """A ``pipelines.entries`` declaration could not be loaded (unreadable file,
    malformed DSL, a duplicate declared name, or a config-key / declared-name
    mismatch). Raised internally by :func:`_load_one_entry` and caught PER-ENTRY
    by :func:`build_pipeline_registry` (logged + durably emitted as a
    ``pipeline_load_failed`` event, then the entry is skipped — see the module
    docstring). Still importable/raisable directly by callers that want the
    old fail-loud posture for a single entry (e.g. an install-time validation
    path that should refuse a bad file rather than silently accept it)."""


def _entry_path(project_root: Path, raw_path: str) -> Path:
    """Resolve an entry's ``path`` — project-root-relative or absolute, mirroring
    the skill entry ``path`` resolution convention."""
    p = Path(raw_path)
    return p if p.is_absolute() else (project_root / p)


def _load_one_entry(
    key: str, raw: dict, project_root: "Path", registry: PipelineRegistry,
) -> None:
    """Load + register ONE ``pipelines.entries.<key>`` declaration.

    Raises :class:`PipelineLoadError` for:
      - a missing ``path``,
      - an unreadable / malformed DSL file (path included),
      - a config entry key that disagrees with its file's declared
        ``pipeline:`` name,
      - a duplicate declared name across entries.

    Callers (:func:`build_pipeline_registry`) catch this per-entry — see the
    module docstring for the visible-but-non-fatal posture. This function
    itself still fails loud; it is the isolation boundary that changed, not
    the validation logic.
    """
    # Deferred import: parser pulls the pipeline executor/schema stack, which is
    # heavier than this loader's own surface — keep module import cheap.
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaRegistry

    raw_path = str(raw.get("path") or "").strip()
    if not raw_path:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r} has no 'path' — a pipeline entry "
            "must declare the DSL file to load."
        )
    path = _entry_path(project_root, raw_path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: could not read {path}: {exc}"
        ) from exc

    schema_registry = SchemaRegistry()
    try:
        pipeline = parse_pipeline_dsl(text, schema_registry)
    except PipelineParseError as exc:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: malformed pipeline file {path}: {exc}"
        ) from exc

    name = pipeline.name
    if not name:
        # parse_pipeline_dsl requires a non-empty ``pipeline:`` name, so
        # this is defensive — a hand-built Pipeline with no name has no
        # key to register under.
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: file {path} produced a pipeline "
            "with no declared name — a 'pipeline:' name is required."
        )
    if name != key:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r} declares path {path} whose DSL "
            f"'pipeline:' name is {name!r} — the config entry key must "
            "match the DSL's declared name exactly (the declared name is "
            "the authoritative key a call/match step resolves against; a "
            "divergent config key is a silent footgun, not a rename)."
        )

    try:
        registry.register(name, pipeline, schema_registry)
    except ValueError as exc:
        # Duplicate declared name across entries — the registry's own
        # re-registration guard. First-registered entry keeps the name; this
        # (later) entry is the one that fails.
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: file {path} declares name {name!r} "
            f"which is already registered by an earlier entry: {exc}"
        ) from exc


def build_pipeline_registry(
    raw_pipelines: "dict[str, Any] | None", project_root: "Path",
    *, strict: bool = False,
) -> PipelineRegistry:
    """Build a populated :class:`PipelineRegistry` from the ``pipelines:`` config.

    For each ``pipelines.entries.<key>`` declaration, ``path`` is resolved
    (project-root-relative or absolute), read, and parsed via
    ``parse_pipeline_dsl`` into a ``Pipeline`` + its own ``SchemaRegistry`` (so a
    registered pipeline's ``verify: schema`` steps resolve), then registered
    under the pipeline's declared ``pipeline:`` name.

    ``raw_pipelines=None`` → an empty registry (the util/no-root path). An
    empty/absent ``pipelines:`` block, or a ``pipelines:`` block with no
    ``entries``, → an empty registry (zero-config default — no pipelines
    registered until the operator or an install tool declares one). These two
    cases return before the per-entry loop even starts — they describe an
    absent config shape, not a failure, so nothing is logged.

    This function itself never raises for a per-entry failure. A missing
    ``path``, an unreadable / malformed DSL file, a config-key / declared-name
    mismatch, or a duplicate declared name are each caught PER ENTRY (via
    :func:`_load_one_entry` raising :class:`PipelineLoadError`), logged as a
    ``logging`` warning, durably emitted as a ``pipeline_load_failed`` P6
    event (``reyn.core.events.events.emit_cli_event`` — see the module
    docstring for why this sink), and then skipped — the remaining entries
    still load normally. A duplicate declared name resolves first-registered-
    wins: the later entry is the one logged/skipped.
    """
    from reyn.core.events.events import emit_cli_event

    registry = PipelineRegistry()
    # ``None`` = the util/no-root path (from_config without a project_root) →
    # never register anything.
    if not isinstance(raw_pipelines, dict):
        return registry

    raw_entries = raw_pipelines.get("entries")
    if not isinstance(raw_entries, dict):
        return registry

    for key, raw in raw_entries.items():
        key = str(key)
        if not isinstance(raw, dict):
            continue  # malformed entry — lenient-default pattern matching skills
        if not bool(raw.get("enabled", True)):
            continue

        try:
            _load_one_entry(key, raw, project_root, registry)
        except PipelineLoadError as exc:
            if strict:
                # Preserve the original fail-loud contract for callers that
                # need atomic all-or-nothing semantics (Session._reapply_pipelines'
                # hot-reload seam — see the docstring's ``strict`` paragraph).
                raise
            logger.warning(
                "pipelines.entries.%r failed to load and was skipped: %s",
                key, exc,
            )
            try:
                emit_cli_event(
                    "pipeline_load_failed",
                    key=key,
                    path=str(raw.get("path") or ""),
                    error=str(exc),
                )
            except Exception:  # noqa: BLE001 -- durable-capture must never crash startup
                pass
            continue

    return registry


__all__ = ["build_pipeline_registry", "PipelineLoadError"]
