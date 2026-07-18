"""Cohesive parameter objects for ``Session.__init__``.

``Session`` accepts a large flat parameter list. The four dataclasses below
group params that are consumed together (same seam, adjacent read sites) so
the constructor signature carries one object per cohesive concern instead of
several loose, individually-optional params. Each field keeps the exact
default/semantics of the flat param it replaces — constructing the object
with no args reproduces today's byte-identical fallback behaviour.

- ``ReactivityConfig``: the three ``reyn.yaml``-resolved config blocks that
  drive the session's reactive surfaces (hooks / composers / fs-watch).
- ``CapabilityScope``: the per-session tool/category/skill visibility
  narrowing surface (catalog exclusions + the resolved
  ``ContextualPermission`` + the enabled skill snapshot).
- ``TaskWiring``: the OS Task subsystem seam (backend instance + waker).
- ``PresentationWiring``: the present-sink / spawn-intervention wiring
  (registry + consumer + bridge).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.runtime.presentation_consumer import PresentationConsumer


@dataclass(frozen=True)
class ReactivityConfig:
    """The resolved ``hooks:`` / ``composers:`` / ``fs_watch:`` config blocks.

    None/absent on any field reproduces the pre-Parameter-Object no-op
    fallback for that seam (empty hook registry, no Composer started, no
    FsWatcher path).
    """

    hooks_config: "object | None" = None
    composers_config: "object | None" = None
    fs_watch_config: "object | None" = None


@dataclass(frozen=True)
class CapabilityScope:
    """The per-session tool/category/skill visibility narrowing surface."""

    exclude_tools: "frozenset[str] | set[str] | None" = None
    excluded_categories: "frozenset[str] | set[str] | None" = None
    contextual_permission: "object | None" = None
    available_skills: Any = None


@dataclass(frozen=True)
class TaskWiring:
    """The OS Task subsystem seam: the session-scoped backend + waker."""

    task_backend: "object | None" = None
    task_waker: "object | None" = None


@dataclass(frozen=True)
class PresentationWiring:
    """The present-sink / spawn-intervention wiring surface."""

    presentation_registry: "object | None" = None
    presentation_consumer: "PresentationConsumer | None" = None
    intervention_bridge: "object | None" = None
