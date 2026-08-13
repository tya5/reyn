"""The ``retrieval`` presentation's Exposure material, shared by both of its cells.

``retrieval`` exists to keep a huge action catalog out of the prompt: the model
is shown a **search affordance** rather than a listing, and the actions it may
call are the ones the search turned up. What that costs is one round trip; what
it buys is a system prompt whose size does not track the catalog's.

★ **The two cells share the FACTS, not the exposed set** — deliberately, and this
is the one place in the arc where that is the right answer rather than a
shortcut. The ``category`` and ``enumerate-all`` presentations each have a single
exposure builder that both of their cells call, with an ``ExposureDeviation``
carrying the difference. Retrieval cannot be written that way, because the two
cells reach the *same* paradigm through structurally different means:

- over ``tool_calls`` the search is a **synthetic, intercepted** affordance
  (``retrieval._search_tool_schema``): ``interpret`` turns the call into a
  ``RePresent`` and the OS swaps the ``tools=`` payload, so the matched actions
  become directly callable **within the same turn**.
- over ``content_fence`` there is no payload to swap. The system prompt — which
  is where this transport's whole tool-use surface lives — is built once per turn
  (``router_loop.py``: ``messages[0]`` is assembled before the iteration loop and
  the ``RePresent`` arm does not rebuild it), so a mid-turn re-presentation would
  have nowhere to land. The cell therefore exposes the **real, dispatchable**
  ``search_actions`` wrapper and lets the narrowing happen at runtime, inside the
  snippet: the model searches and then calls what it found, through
  ``invoke_action``, without the OS re-presenting anything.

Folding those two into one builder would mean a parameter meaning "does this cell
intercept the search or dispatch it", which is a per-cell composer wearing an
``ExposureDeviation``'s clothes — the shape ``reyn.tools.encoders`` says not to
build. So the builders stay apart and this module holds what genuinely is
common: the transport-neutral ``sp_facts``.

Recorded because absence cannot distinguish "forgotten" from "decided": the
asymmetry above is a decision, and the sentence that would falsify it is "the OS
rebuilds the system prompt on a RePresent round" — if that ever becomes true, the
``content_fence`` cell can adopt the intercepted form and the builders can merge.
"""
from __future__ import annotations

from typing import Any

from reyn.tools.schemes._discovery import tier_wants_discovery_mandate


def retrieval_sp_facts(layer_ctx: Any) -> "dict[str, object]":
    """The transport-neutral facts a transport needs to shape retrieval's tool-use
    system prompt. Facts only — the rendering is the encoder's.

    Retrieval is always ``universal_wrappers_enabled=False``: the OS's named-gate
    "## Action categories" block describes a wrapper vocabulary this presentation
    does not lead with. ``search_actions_enabled`` is derived from
    ``search_visible`` (the D14 gate), so the two cells cannot disagree about
    whether the search affordance is live."""
    return {
        "universal_wrappers_enabled": False,
        "search_actions_enabled": bool(layer_ctx.get("search_visible", False)),
        "discovery_mandate": tier_wants_discovery_mandate(layer_ctx.get("router_model")),
        "non_interactive": bool(layer_ctx.get("non_interactive", False)),
        # #2548 PR-A: skill registry snapshot → ## Skills block (rendered into
        # the DEDICATED slot_post_skills, so the slot_post_catalog override
        # does NOT clobber it).
        "available_skills": layer_ctx.get("available_skills"),
    }


__all__ = ["retrieval_sp_facts"]
