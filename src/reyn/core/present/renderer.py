"""PresentationRenderer protocol — the surface seam a `present` op result reaches (FP-0054 PR-B).

`op_runtime/present.py::handle` computes a `ResolvedPresentation` (bound, neutralized,
capped render model) and hands it to `OpContext.presentation_renderer` when one is wired —
`None` means the PR-A null-surface behavior (no UI reached, `surface="null"`). A wired
renderer names its own `surface_name` (fed to `resolve_bindings(..., surface=...)` so the
guard's per-surface neutralizer strategy matches the actual sink — see `core/present/guard.py`).

`op_runtime` never imports a UI toolkit (Rich, prompt_toolkit): a concrete renderer (e.g. the
inline-CUI's `OutboxPresentationRenderer` in `runtime/session_buses.py`) only needs to get the
render model to its surface — the actual Rich-conversion happens downstream in the UI layer
(`interfaces/repl/renderer.py`), the same layering every other outbox-carried message already
uses.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from reyn.core.present.binding import ResolvedPresentation


@runtime_checkable
class PresentationRenderer(Protocol):
    """A surface that can receive a resolved presentation. Fire-and-continue: `render`
    does not return a value the caller awaits on — the `present` op has already produced
    its ack from `resolved`'s stats before calling this."""

    surface_name: str

    def render(self, resolved: "ResolvedPresentation") -> None: ...
