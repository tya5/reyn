"""Pipeline registry — name -> Pipeline lookup for the ``run_pipeline`` tool (IS-1).

IS-1 (``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R6) is SYNC +
REGISTERED only: a pipeline must already be registered under a name before an
agent can launch it via ``run_pipeline(name, input)``. Registration is
PROGRAMMATIC in this slice — an implementer/test builds a
:class:`~reyn.core.pipeline.executor.Pipeline` dataclass directly and calls
:meth:`PipelineRegistry.register`. A YAML DSL parser that produces ``Pipeline``
instances from a pipeline definition file is a later slice; once it lands, real
pipelines register through it instead of by hand, but this registry's
lookup contract (name -> Pipeline) does not change.

This is deliberately a plain in-memory mapping, not a process-wide singleton:
callers (a session, a test) construct their own ``PipelineRegistry`` and thread
it through explicitly (mirrors how ``AgentRegistry`` / ``StateLog`` are threaded
rather than reached via a hidden global), keeping registrations scoped to the
owner that created them.

(#2572) A registered pipeline may also carry a
:class:`~reyn.core.pipeline.schema.SchemaRegistry` alongside it — a bare
``name -> Pipeline`` map has no other home for the schemas its ``verify:
schema`` steps validate against, unlike an inline launch (whose schemas live
in the same DSL string that parses into the ``Pipeline``). ``register``'s
``schema_registry`` param and :meth:`PipelineRegistry.get_schema_registry`
close that gap; the registered launch handlers thread the retrieved registry
into ``start_pipeline_run``/``run_pipeline_attached`` alongside the pipeline
itself.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.core.pipeline.executor import Pipeline
    from reyn.core.pipeline.schema import SchemaRegistry


class PipelineNotFoundError(KeyError):
    """Raised by :meth:`PipelineRegistry.get` when ``name`` has no registered
    :class:`~reyn.core.pipeline.executor.Pipeline`. A ``KeyError`` subclass so
    callers that already handle ``KeyError`` (dict-lookup idiom) catch this
    too, while still giving ``run_pipeline`` a specific type to match on for
    a clear "pipeline not found" tool error (rather than a generic KeyError
    with just the bare name in its args)."""


class PipelineRegistry:
    """In-memory ``name -> Pipeline`` registry.

    ``register``/``get`` are the whole contract for IS-1 — no update/unregister
    (re-registration under the same name is rejected, same shadowing-prevention
    posture as :class:`~reyn.tools.registry.ToolRegistry`)."""

    def __init__(self) -> None:
        self._pipelines: "dict[str, Pipeline]" = {}
        self._schema_registries: "dict[str, SchemaRegistry | None]" = {}

    def register(
        self, name: str, pipeline: "Pipeline",
        schema_registry: "SchemaRegistry | None" = None,
    ) -> None:
        """Register ``pipeline`` under ``name``. Re-registration under an
        already-used name raises ``ValueError`` (prevents accidentally
        shadowing a previously registered pipeline).

        ``schema_registry`` (#2572, additive/optional — a registered pipeline
        with no ``verify: schema`` steps needs none) carries the schemas a
        registered pipeline's ``verify: schema`` steps validate against; a
        plain name -> Pipeline map has no other home for them. Retrieve it via
        :meth:`get_schema_registry`."""
        if name in self._pipelines:
            raise ValueError(
                f"a pipeline is already registered under name {name!r}; "
                "re-registration is not allowed — remove the prior "
                "registration first."
            )
        self._pipelines[name] = pipeline
        self._schema_registries[name] = schema_registry

    def get(self, name: str) -> "Pipeline":
        """Look up the ``Pipeline`` registered under ``name``.

        Raises :class:`PipelineNotFoundError` (a ``KeyError``) when absent —
        the ``run_pipeline`` tool handler catches this to produce a clear
        "pipeline not found" tool error rather than an unhandled exception."""
        try:
            return self._pipelines[name]
        except KeyError:
            raise PipelineNotFoundError(name) from None

    def get_schema_registry(self, name: str) -> "SchemaRegistry | None":
        """The ``SchemaRegistry`` registered alongside ``name`` (#2572), or
        ``None`` when the pipeline was registered without one.

        Raises :class:`PipelineNotFoundError` when ``name`` itself is not
        registered — same contract as :meth:`get`."""
        if name not in self._pipelines:
            raise PipelineNotFoundError(name)
        return self._schema_registries[name]

    def names(self) -> "tuple[str, ...]":
        """All registered pipeline names (for introspection / future
        surfacing, e.g. an L1-style list like the skill registry's)."""
        return tuple(self._pipelines)

    def entries(self) -> "tuple[tuple[str, str], ...]":
        """``(name, description)`` for every registered pipeline (IS-5:
        consumed by the universal catalog's ``pipeline`` category
        enumerator so ``list_actions(category=["pipeline"])`` surfaces
        each registered pipeline's name + description to the LLM)."""
        return tuple(
            (name, pipeline.description) for name, pipeline in self._pipelines.items()
        )


__all__ = ["PipelineRegistry", "PipelineNotFoundError"]
