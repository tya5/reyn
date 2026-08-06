"""How much room the transient regions may take on a short terminal (#3680).

The zone above the composer stacks: an intervention panel, the ``/rewind``
picker, the live-turn row, the sent queue, the completion popup, the search
bar — each with its own cap, and none of them aware of the others. Measured at
80x20 with three messages queued, a turn running and the Help drawer open, the
conversation was left **one row**. Every region was within its own limit.

So the caps have to be decided together, against the height actually
available, rather than one at a time. This module is that decision, as a pure
function of the terminal height and which regions want space — pure so the
policy can be reasoned about and tested without mounting anything, and so the
same numbers can be asserted directly rather than inferred from a screenshot.

The order regions give way in is the issue's, and it is an order of
consequence, not of size:

1. the composer keeps its three rows — losing the line you are typing is worse
   than losing anything else;
2. the conversation keeps :data:`FLOW_MIN` — below that it stops being a
   conversation and becomes a viewport;
3. a pending intervention stays visible and operable — it is a question
   somebody is waiting on;
4. the completion popup, the rewind picker and the drawer shrink, in that
   order, because each still holds every item it had: they scroll (#3688,
   #3699), so a smaller box hides nothing;
5. the sent queue collapses to a count only as a last resort, and never drops
   an item — the queue is durable state, and #3688 is the record of what
   happens when a region silently stops showing part of it.
"""
from __future__ import annotations

#: Rows the conversation must keep. Eight is the acceptance figure from #3680:
#: enough for a short exchange to remain readable as an exchange rather than a
#: peephole onto the newest line.
FLOW_MIN = 8

#: Rows the composer must keep (its content line plus its two rules).
COMPOSER_MIN = 3

#: What each region asks for when nothing is squeezing it — the caps that were
#: already in the stylesheets before this policy existed.
DRAWER_MAX = 12
REWIND_MAX = 12
COMPLETION_MAX = 10
QUEUE_MAX = 6


def compact_caps(
    screen_height: int,
    *,
    drawer_open: bool = False,
    rewind_open: bool = False,
    completion_open: bool = False,
    queue_items: int = 0,
    turn_active: bool = False,
    intervention_open: bool = False,
) -> "dict[str, int]":
    """Height caps for the transient regions, given the room there is.

    Returns a cap per region name. ``queue`` of ``0`` means the queue should
    render as its one-line ``Queued: N`` summary instead of a row per item —
    still every item, still cancellable, just not spending a row each.

    Only regions that are actually open take part: a closed drawer is not
    competing for anything, so a terminal that is short but uncluttered is
    left alone.
    """
    fixed = COMPOSER_MIN + _CHROME_ROWS
    if turn_active:
        fixed += 1  # the live-turn row
    if intervention_open:
        fixed += _INTERVENTION_ROWS

    spare = screen_height - fixed - FLOW_MIN
    caps = {
        "drawer": DRAWER_MAX if drawer_open else 0,
        "rewind": REWIND_MAX if rewind_open else 0,
        "completion": COMPLETION_MAX if completion_open else 0,
        "queue": min(queue_items, QUEUE_MAX) if queue_items else 0,
    }

    # Give way in reverse order of consequence: the completion popup first (it
    # is mid-keystroke and scrolls), then the picker, then the drawer, and the
    # queue's own rows only once the others are at their floor.
    for name, floor in (("completion", 3), ("rewind", 3), ("drawer", 3)):
        if spare >= sum(caps.values()):
            break
        excess = sum(caps.values()) - spare
        if caps[name] > floor:
            caps[name] = max(floor, caps[name] - excess)

    if sum(caps.values()) > spare and caps["queue"]:
        # Last: the queue becomes a count. It keeps every item — this is the
        # one region whose contents are durable state a person is waiting on,
        # so it may lose its rows but never an entry (#3688).
        caps["queue"] = 0

    # Last resort, and the issue's own priority 7: a region that cannot be made
    # small enough to leave a readable conversation is CLOSED rather than left
    # as a sliver. A two-row drawer is not a smaller drawer, it is an unusable
    # one that has also taken the conversation with it — and closing is
    # reversible by the same keystroke that opened it, on a terminal where
    # reopening at a workable height is a resize away.
    for name in ("completion", "rewind", "drawer"):
        if sum(caps.values()) > spare and caps[name]:
            caps[name] = 0

    return caps


#: Rows the always-present chrome occupies: the menu row and the status row it
#: packs onto, plus the rule above the input.
_CHROME_ROWS = 3

#: Rows a pending intervention keeps — its prompt and one answer line. It is
#: never squeezed below this: it is a question somebody is waiting on.
_INTERVENTION_ROWS = 4
