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
this loader FAILS LOUD on that mismatch rather than silently registering under
one or the other (same posture as the install op's op.name vs declared-name
check in ``op_runtime/pipeline_install.py``).

Failure posture is FAIL-LOUD (not silent-skip): a malformed DSL file raises with
its path (a typo must not silently drop a pipeline the operator meant to ship),
a duplicate declared name across entries surfaces the registry's own
re-registration ``ValueError``, and a config-key / declared-name mismatch raises
explicitly. ``raw_pipelines=None`` (the util/no-root path) → an empty registry;
an empty/absent ``pipelines:`` block or an empty ``entries`` map → an empty
registry (zero pipelines is a valid, non-error state — same as skills).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reyn.core.pipeline.registry import PipelineRegistry


class PipelineLoadError(ValueError):
    """A ``pipelines.entries`` declaration could not be loaded (unreadable file,
    malformed DSL, a duplicate declared name, or a config-key / declared-name
    mismatch). Carries enough detail for the operator to find the offending
    entry — fail-loud, never silent-skip."""


def _entry_path(project_root: Path, raw_path: str) -> Path:
    """Resolve an entry's ``path`` — project-root-relative or absolute, mirroring
    the skill entry ``path`` resolution convention."""
    p = Path(raw_path)
    return p if p.is_absolute() else (project_root / p)


def build_pipeline_registry(
    raw_pipelines: "dict[str, Any] | None", project_root: "Path",
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
    registered until the operator or an install tool declares one).

    Raises :class:`PipelineLoadError` for:
      - an unreadable / malformed DSL file (path included),
      - a duplicate declared name across entries,
      - a config entry key that disagrees with its file's declared
        ``pipeline:`` name (fail-loud rather than silently picking one).
    """
    # Deferred import: parser pulls the pipeline executor/schema stack, which is
    # heavier than this loader's own surface — keep module import cheap.
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
    from reyn.core.pipeline.schema import SchemaRegistry

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
            # Duplicate declared name across entries — surface loudly with the
            # path (the registry's re-registration guard).
            raise PipelineLoadError(
                f"pipelines.entries.{key!r}: file {path} declares name {name!r} "
                f"which is already registered by an earlier entry: {exc}"
            ) from exc

    return registry


__all__ = ["build_pipeline_registry", "PipelineLoadError"]
