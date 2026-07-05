"""reyn.data.pipelines.registry — disk → PipelineRegistry loader (#2575).

The production population path for the pipeline feature. Before #2575 a
``Session`` owned an EMPTY :class:`~reyn.core.pipeline.registry.PipelineRegistry`
(``run_pipeline`` had a live registry to look up against, but nothing ever
registered into it in production — registration was programmatic/test-only),
so no pipeline was launchable in a real session. This loader closes that gap by
mirroring the skill loader (``reyn.data.skills.registry.build_skill_registry``):
an operator drops Appendix-B DSL ``*.yaml`` files into a scanned directory
(default ``pipelines/`` at the project root, sibling to ``skills/`` — operator-
owned SOURCE, NOT ``.reyn/`` run-authored state per reyn-dir-layout.md) and each
is parsed + registered at session-factory time.

Unlike skills (explicit ``entries`` config, metadata only, never parses the
file), a pipeline must be PARSED to be usable — the ``PipelineRegistry`` stores
live ``Pipeline`` objects and surfaces their name+description to the LLM (IS-5's
D19 catalog enumerator). So this loader parses each file via
:func:`~reyn.core.pipeline.parser.parse_pipeline_dsl` and registers the result
under the pipeline's OWN declared ``pipeline:`` name (``Pipeline.name``, #2575) —
authoritative because that is the identity a ``call``/``match`` step's
``pipeline: LIT`` resolves against. The file name is just a container: a
``greet.yaml`` declaring ``pipeline: hello`` registers under ``hello`` (and is
callable as ``hello``), not ``greet``.

Failure posture is FAIL-LOUD (not silent-skip): a malformed DSL file raises with
its path (a typo must not silently drop a pipeline the operator meant to ship),
and a duplicate declared name across files surfaces the registry's own
re-registration ``ValueError``. ``raw_pipelines=None`` (the util/no-root path)
→ an empty registry; an empty/absent ``pipelines:`` block still scans the
default ``pipelines/`` dir (zero-config UX), yielding an empty registry only
when that dir is absent/empty.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reyn.core.pipeline.registry import PipelineRegistry

# Default directories scanned for pipeline DSL files, project-root-relative.
# Mirrors the skills ``scan_dirs: ["skills"]`` documented default.
_DEFAULT_SCAN_DIRS: "tuple[str, ...]" = ("pipelines",)


class PipelineLoadError(ValueError):
    """A pipeline DSL file under a scan dir could not be loaded (unreadable,
    malformed DSL, or a duplicate declared name). Carries the offending file
    path so the operator can find it — fail-loud, never silent-skip."""


def _scan_dirs(raw_pipelines: "dict[str, Any]") -> "list[str]":
    """The configured scan dirs, or the default. A non-list / empty ``scan_dirs``
    falls back to the default (lenient, like the skills builder)."""
    raw = raw_pipelines.get("scan_dirs")
    if not isinstance(raw, list) or not raw:
        return list(_DEFAULT_SCAN_DIRS)
    return [str(d) for d in raw]


def build_pipeline_registry(
    raw_pipelines: "dict[str, Any] | None", project_root: "Path",
) -> PipelineRegistry:
    """Build a populated :class:`PipelineRegistry` from the ``pipelines:`` config.

    For each configured (or default) scan dir under ``project_root``, every
    ``*.yaml`` file (sorted, for deterministic registration order) is read and
    parsed via ``parse_pipeline_dsl`` into a ``Pipeline`` + its own
    ``SchemaRegistry`` (so a registered pipeline's ``verify: schema`` steps
    resolve), then registered under the pipeline's declared ``pipeline:`` name.

    ``raw_pipelines=None`` → an empty registry (the util/no-root path). An
    empty/absent ``pipelines:`` block still scans the default ``pipelines/``
    dir (zero-config UX) — empty only when the dir is absent/empty.

    Raises :class:`PipelineLoadError` for a malformed / unreadable file or a
    duplicate declared name — fail-loud with the path, never silent-skip.
    """
    # Deferred import: parser pulls the pipeline executor/schema stack, which is
    # heavier than this loader's own surface — keep module import cheap.
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaRegistry

    registry = PipelineRegistry()
    # ``None`` = the util/no-root path (from_config without a project_root) →
    # never scan. An empty dict ``{}`` = the DEFAULT config (no ``pipelines:``
    # block declared) → STILL scan the default ``pipelines/`` dir, so dropping a
    # file in ``pipelines/`` needs zero config (the intended zero-config UX).
    if not isinstance(raw_pipelines, dict):
        return registry

    for rel_dir in _scan_dirs(raw_pipelines):
        scan_dir = (project_root / rel_dir).resolve()
        if not scan_dir.is_dir():
            # A configured dir that doesn't exist yet is not an error (the
            # operator may add files later) — nothing to load from it.
            continue
        for path in sorted(scan_dir.glob("*.yaml")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PipelineLoadError(
                    f"could not read pipeline file {path}: {exc}"
                ) from exc
            schema_registry = SchemaRegistry()
            try:
                pipeline = parse_pipeline_dsl(text, schema_registry)
            except PipelineParseError as exc:
                raise PipelineLoadError(
                    f"malformed pipeline file {path}: {exc}"
                ) from exc
            name = pipeline.name
            if not name:
                # parse_pipeline_dsl requires a non-empty ``pipeline:`` name, so
                # this is defensive — a hand-built Pipeline with no name has no
                # key to register under.
                raise PipelineLoadError(
                    f"pipeline file {path} produced a pipeline with no declared "
                    "name — a 'pipeline:' name is required to register it"
                )
            try:
                registry.register(name, pipeline, schema_registry)
            except ValueError as exc:
                # Duplicate declared name across files — surface loudly with the
                # path (the registry's re-registration guard).
                raise PipelineLoadError(
                    f"pipeline file {path} declares name {name!r} which is "
                    f"already registered by an earlier file: {exc}"
                ) from exc
    return registry


__all__ = ["build_pipeline_registry", "PipelineLoadError"]
