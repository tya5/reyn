"""Shared display-frame ``meta`` key constants for the coalesced tool-call shape.

:data:`RESULT_KIND_KEY` / :data:`RESULT_META_KEY` are the two meta keys that
mark a ``tool_call_started`` display frame as SETTLED (coalesced) — i.e. its
result has been folded into the same entry rather than appended as a separate
row. Two producers stamp this shape and must agree on the exact key strings:

- ``app.py``'s ``_coalesce_tool_result`` (the LIVE path — a RUNNING started
  entry settles in place when its completion frame arrives).
- ``restore.py``'s :func:`~reyn.interfaces.inline.textual_chat.restore.project_restored_frames`
  (the RESTORE path — a persisted tool result is projected straight into the
  same coalesced shape so a restored turn reads identically to a live one).

``restore.py`` MUST stay import-pure (no ``textual`` / ``textual_flowview`` —
see its module docstring), so these two plain ``str`` constants live in this
tiny textual-free module rather than in ``presenter.py`` (which imports
``textual_flowview``). ``presenter.py`` re-exports the same names so existing
importers are unaffected.
"""
from __future__ import annotations

#: Present on a ``tool_call_started`` frame's ``meta`` once it has settled —
#: the value is the ORIGINAL completion frame's ``kind``
#: (``"tool_call_completed"`` / ``"tool_call_failed"``).
RESULT_KIND_KEY = "_result_kind"

#: Present alongside :data:`RESULT_KIND_KEY` — the ORIGINAL completion
#: frame's ``meta`` (carries ``result`` / ``error_message`` / ``error_kind``).
RESULT_META_KEY = "_result"

__all__ = ["RESULT_KIND_KEY", "RESULT_META_KEY"]
