"""Negative examples for the tool-use (scheme x transport) axes — the marked,
non-expiring kind.

**The rule these constants exist to enforce** (#3376, architect's formulation):

> A negative example must come from **outside the space the system extends
> into** — something that cannot ENTER the set, not something that merely is not
> in the set yet.

The #3376 arc is the natural experiment. Every witness taken from *inside* the
presentation axis expired on schedule, because the arc's entire purpose was to
register the cells those witnesses pointed at:

- three tests pinned ``(category, content_fence)`` as *the* unregistered cell.
  P2 registered it and all three went RED. They were retargeted to
  ``(retrieval, content_fence)``.
- P3 registered that one. The same three would have gone RED again.

A pair like ``(retrieval, content_fence)`` was never forbidden — it was a
**legal** combination that had not arrived. "Not in the set" and "cannot be in
the set" look identical at the call site and behave completely differently over
time; this module holds the second kind.

``NOT_A_PRESENTATION`` is not a value of the presentation axis at all, so no
future cell can register it — the property
``tests/test_fp0066_p4a_transport_axis_3247.py`` asserts directly, which is what
turns "outside the namespace" from a comment into a checked claim.

**The mark is the point.** A negative example is written exactly like a positive
one, so nothing can tell them apart by inspection — which is why the architect
ruled a syntactic gate impossible without one. Importing the witness from this
module *is* the mark: a reviewer grepping for negative examples finds every user
of these names, and a test that hardcodes its own literal instead is visibly not
using the shared, gated one.

**If you must use an expiring witness**, keep it out of here and declare its
expiry inline where it is used, in falsifiable form (``comments.md`` §4): say
what registers it and what breaks when that happens. A permanent witness is
always preferred.
"""
from __future__ import annotations

#: A presentation-axis name that is not on the axis. Refused by
#: ``resolve_scheme_for_transport`` for EVERY transport, and cannot stop being
#: refused: registering it would mean adding a presentation literally called
#: "no-such-presentation".
NOT_A_PRESENTATION = "no-such-presentation"

__all__ = ["NOT_A_PRESENTATION"]
