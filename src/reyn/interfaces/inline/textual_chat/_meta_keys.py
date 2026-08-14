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

#: Present (and truthy) on a settled tool frame while its row is expanded —
#: the app stamps/clears it on Space (:meth:`_CursorFlowView.action_toggle_fold`,
#: #4697/#4691§6), and the presenter renders the FULL result instead of the
#: one-line summary while it is set (#3508).
#:
#: It lives on the ITEM rather than in the view because ``FlowPresenter.present``
#: is contractually pure with respect to ``(item, width)``: "expanded" has to be
#: part of the item's state for a re-present to be legitimate, and
#: ``Entry.update()`` is what tells the view that state changed. A flag held in
#: the view instead would make ``present`` return different bodies for the same
#: item — exactly what the contract forbids.
EXPANDED_KEY = "_expanded"

#: A sentinel value for :data:`RESULT_KIND_KEY` marking a RUNNING tool row that
#: was force-settled at the TURN BOUNDARY because no completion frame ever
#: arrived — an orphan (#72). Distinct from a real completion frame's kind
#: (``"tool_call_completed"`` / ``"tool_call_failed"``) so the presenter can
#: render it NEUTRAL: neither a success nor a failure, just "no result ever
#: came". Stamped by ``app.py``'s ``_sweep_orphaned_running_tools`` (the live
#: path only — there is no restore-path equivalent: a persisted turn is, by
#: definition, one that already settled).
ORPHANED_RESULT_KIND = "_orphaned_no_result"

#: Present on a ``tool_call_started`` frame's ``meta`` WHILE it is RUNNING —
#: the monotonic START timestamp (app-computed; tool frames carry no
#: elapsed/progress of their own, ADR finding D2, #3283). Stamped by
#: ``app.py``'s ``_begin_running_indicator``; the presenter's live spinner +
#: elapsed body (Phase ②) and the flowview right-gutter's live elapsed
#: decorator (Phase ④) both key off its PRESENCE. Stripped once the row
#: settles — see :data:`ELAPSED_SECS_KEY`, which captures the final value
#: at that same moment, before this key is removed.
RUNNING_SINCE_KEY = "_running_since"

#: Present on a SETTLED ``tool_call_started`` frame's ``meta`` — the final
#: elapsed seconds (``int``), computed at settle time from
#: :data:`RUNNING_SINCE_KEY` before that key is stripped (``app.py``'s
#: ``_coalesce_tool_result`` / ``_sweep_orphaned_running_tools``, #3283 Phase
#: ④). **LIVE-SESSION ONLY, by decision**: ``restore.py``'s
#: ``project_restored_frames`` never stamps this key. A persisted
#: ``ChatMessage`` / ``history.jsonl`` record carries NO timing field at all
#: (checked: no elapsed/duration meta key exists in
#: ``reyn.runtime.chat_message``, and ``router_loop.py``'s tool-result
#: assembly never stamps one at persist time). Making elapsed survive restore
#: would mean widening the PERSISTED shape of every tool-result
#: ``ChatMessage`` — a reyn-wide persistence change, disproportionate to a
#: TUI gutter decoration, and out of step with this arc's own precedent of
#: keeping ①②④ TUI-local while the one genuinely cross-layer phase (③, token
#: streaming) was split into its own issue. So a restored row's right gutter
#: shows NOTHING for elapsed — never a stale, reconstructed, or approximated
#: value. This is a decision (owner-adjudicated on #3283), not an oversight.
ELAPSED_SECS_KEY = "_elapsed_secs"

#: Marks a row as one pipeline RUN'S progress rather than a one-off status
#: line, and carries the run id. Set app-side when a step frame is folded in
#: (``_coalesce_pipeline_step``); the presenter reads it to render the run's
#: state instead of the latest frame's text.
PIPELINE_RUN_KEY = "_pipeline_run_id"

__all__ = [
    "ELAPSED_SECS_KEY",
    "PIPELINE_RUN_KEY",
    "ORPHANED_RESULT_KIND",
    "RESULT_KIND_KEY",
    "EXPANDED_KEY",
    "RESULT_META_KEY",
    "RUNNING_SINCE_KEY",
]
