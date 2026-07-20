"""#3193: single classifier for ``execute_op`` result ``status`` values.

Problem this closes: several ``Session._file_*`` wrappers (session.py)
whitelisted ``status == "ok"`` (plus, for read, ``"not_found"``) and
collapsed every other status — most damagingly ``"truncated"`` — into a
lying ``{"error": "read failed"}``, discarding content that was actually
read successfully.

``status`` is an OPEN set: a repo-wide sweep of every ``op_runtime/*.py``
handler found 15 distinct literal values in use, with no typed enum backing
them (enumerating and typing that set is a separate, larger arc — out of
scope here, see #3193's discussion). A per-wrapper whitelist of "the known
good ones today" reproduces the exact same hole the moment a 16th value
is introduced anywhere in op_runtime; a fresh wrapper written tomorrow
would have no way to know it needs updating.

This module is the ONE place that maps a status string to an outcome
class. Every ``Session._file_*`` wrapper (and any future one) should
import :func:`classify_op_status` rather than re-deriving its own
success/failure test.

Classification policy
----------------------
- ``success``  — the op fully completed; the result carries the complete,
  requested content (``"ok"``, and the plugin-install-lifecycle terminal
  ``"installed"``).
- ``partial``  — the op completed but the result is INCOMPLETE by
  construction (``"truncated"`` — a read or glob capped by a size/count
  limit). Content IS present and must reach the caller; so must the
  incompleteness signal (``note`` / ``truncated`` / ``next_offset`` / etc.)
  — the caller must be able to tell this apart from ``success``.
- ``failure``  — the op did not produce usable content at all (permission
  denial, not-found, a raised exception, a user/tool refusal, a budget/size
  cap that produced NOTHING, an unresolved dependency, ...). Enumerated
  exhaustively below from the current op_runtime vocabulary.
- ``unknown``  — a status string this module does not recognize. This is
  the crux of the (b) design direction: an unrecognized status must NOT be
  silently folded into ``failure`` (that is exactly how "truncated" content
  got thrown away and reported as "read failed" — a lie). It is also not
  silently folded into ``success`` (an unrecognized status could just as
  easily mean a new failure mode with content-shaped keys that happen to be
  absent). Callers must treat ``unknown`` as "an outcome I cannot classify
  yet" and say so plainly to their own caller/the LLM (e.g. surface
  whatever payload fields ARE present, tag the result as carrying an
  unrecognized status, and log it) — never fabricate certainty in either
  direction. `tests/test_3193_op_status_classifier_coverage.py` is the CI
  gate that keeps this module's known-status tables in sync with the live
  op_runtime vocabulary, so the ``unknown`` branch is a should-never-happen
  safety net, not a load-bearing default path.
"""
from __future__ import annotations

from typing import Literal

# "ok": every op kind's happy path.
# "installed": plugin/mcp/skill/pipeline/presentation install ops' terminal
#   success status (distinct literal from "ok" because the payload shape
#   differs — install ops report `{"status": "installed", ...}`).
# "installing": NOT an execute_op result status returned to a caller — it is
#   a persisted on-disk install-state marker plugin_install.py writes to a
#   `.reyn-plugin` state file mid-install (json.dumps'd, not returned).
#   Classified here anyway (as a non-terminal, non-failure state) purely so
#   the AST completeness gate — which enumerates every literal
#   `"status": "..."` dict entry in op_runtime regardless of whether it is
#   ever handed back as an op result — doesn't have to special-case it.
KNOWN_SUCCESS_STATUSES: frozenset[str] = frozenset({"ok", "installed", "installing"})

# "truncated": read (line/char-window cap) and glob/list_directory
# (max_results cap) both use this to mean "content is real and present,
# but it's a capped subset — say so instead of silently dropping the rest."
KNOWN_PARTIAL_STATUSES: frozenset[str] = frozenset({"truncated"})

# Every other literal status string found across src/reyn/core/op_runtime/
# (grep-verified, #3193): all mean "no usable content was produced" —
# a permission denial, a not-found, a raised exception surfaced as a
# structured error, a user/tool refusal, a request the OS itself declined
# to start (blocked/cancelled/timeout/too_large/needs_secrets), or a
# dispatch-layer short-circuit (skipped — no handler / OpSkipped;
# uninstalled — a plugin/mcp/skill/pipeline/presentation removal's terminal
# status, which is a "removed" confirmation with no further content, not a
# read/write result, but carries no partial content either).
KNOWN_FAILURE_STATUSES: frozenset[str] = frozenset({
    "not_found",
    "denied",
    "error",
    "blocked",
    "cancelled",
    "refused",
    "timeout",
    "too_large",
    "needs_secrets",
    "skipped",
    "uninstalled",
})

ALL_KNOWN_STATUSES: frozenset[str] = (
    KNOWN_SUCCESS_STATUSES | KNOWN_PARTIAL_STATUSES | KNOWN_FAILURE_STATUSES
)

OpStatusClass = Literal["success", "partial", "failure", "unknown"]


def classify_op_status(status: object) -> OpStatusClass:
    """Classify an ``execute_op`` result's ``status`` value.

    ``status`` is typed ``object`` (not ``str``) because callers pass
    ``result.get("status")`` on an untyped dict — a missing/non-string
    status is exactly the kind of malformed input this function must
    handle as ``"unknown"``, not raise on.
    """
    if status in KNOWN_SUCCESS_STATUSES:
        return "success"
    if status in KNOWN_PARTIAL_STATUSES:
        return "partial"
    if status in KNOWN_FAILURE_STATUSES:
        return "failure"
    return "unknown"
