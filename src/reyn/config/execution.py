"""reyn.config.execution — execution config: Plan/TimeTravel/ToolUse. (#1682 #3 split)."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reyn.runtime.budget.budget import CostConfig, CostLimitConfig


@dataclass
class ToolUseConfig:
    """``tool_use:`` — the chat-layer tool-use scheme x transport selector
    (#1593; #2768; FP-0066 P4b, #3247).

    Two orthogonal axes (FP-0066 §2 / P4 firm §1-§2): ``scheme`` is the
    PRESENTATION (how capabilities are shown/discovered to the LLM —
    ``category`` / ``enumerate-all`` / ``retrieval``) and ``transport`` is how
    the model expresses the chosen action (``tool_calls`` / ``content_fence``).
    Before P4b these were conflated into one flat name (the retired ``chat``
    key), with ``codeact`` registered as if it were a 4th sibling scheme when
    it is really ``enumerate-all`` presentation over the ``content_fence``
    transport. #1657: the default (``scheme=enumerate-all``,
    ``transport=tool_calls``) is the owner H1 fix — flat-listing actions
    stops invoke_action name-hallucination, 30%→100% direct tool-use.

    ``tool_use.chat`` is REMOVED, clean-break (no compat alias) — a reyn.yaml
    still carrying it fails loud at parse time naming the migration (P4 firm
    §2 J2; see ``_build_tool_use_config``). Not every (scheme, transport)
    combination is implemented; the pair is validated through
    ``reyn.tools.transport.resolve_scheme_for_transport`` at parse time (P4a's
    valid-pair registry becomes the live validation authority). #2768 removed
    the dead ``step`` / ``phase`` layers (phase-graph era — zero read sites;
    ``PhaseRouterLoopHost`` deleted in #2438).

    ``universal_wrappers_enabled`` (#4552 PR-3): moved here from
    ``action_retrieval.universal_wrappers_enabled`` — architect's ruling
    ("wrappers_enabled is a tool_use property, not a retrieval setting"),
    unblocked once #4564 fixed the flag's ONE remaining undeclared reach
    (search_actions visibility, now solely ``embedding.enabled``'s). What's
    left is exactly what this field's own name says: for a layer whose
    ``scheme`` resolves to ``universal-category``, ``true`` (default)
    exposes the 3 universal-category wrapper functions (``list_actions`` /
    ``describe_action`` / ``invoke_action``) in that layer's ``tools=``,
    instead of the flat legacy shape. Has no effect under any other
    scheme — a config with ``scheme`` != ``universal-category`` and this
    flag explicitly ``true`` is reported (never raised — see below) by
    ``reyn config validate``'s ``disabled_config_keys`` (#4231(C),
    relocated here from ``action_retrieval.*`` in the same PR, not
    superseded — #4564 only removed the search_actions coupling, the
    scheme-mismatch inconsistency itself is unchanged and still real).

    Deliberately validated SOFT here, unlike this class's OWN sibling
    fields (``chat`` raises; an invalid (scheme, transport) pair raises):
    an explicit, standing owner ruling governs config validation UNIFORMLY
    ("warn, never hard-fail, anywhere — including sandbox.policy, no
    special case", ``loader.py``'s ``_warn_unknown_config_keys``
    docstring) and takes precedence over this class's own local
    convention. The inconsistency this field can produce degrades to a
    silent no-op (post-#4564: scheme != universal-category simply never
    reads it, nothing crashes or misbehaves) — matching the ruling's own
    "warn" category, not its exceptions. A future reader who notices this
    field is validated differently from ``scheme``/``transport`` in the
    SAME dataclass should read that as owner-ruling-over-local-convention,
    not as an oversight.
    """

    scheme: str = "enumerate-all"
    transport: str = "tool_calls"
    universal_wrappers_enabled: bool = True


def _build_tool_use_config(raw: object) -> ToolUseConfig:
    """Parse ``tool_use:`` from reyn.yaml. None / missing / empty → default
    (scheme=enumerate-all, transport=tool_calls — #1657).

    ``scheme`` and ``transport`` each accept a name (string); a missing key
    keeps its default. A non-mapping block or non-string value is a config
    error (fail loud). The old ``chat`` key is REMOVED (FP-0066 P4b,
    clean-break) — its presence is detected and raises a legible migration
    error rather than being silently ignored (P4 firm §2 J2: a silently
    dropped old key is a "config that doesn't take effect" trap). The
    resulting (scheme, transport) pair is validated through P4a's
    ``resolve_scheme_for_transport`` — an unregistered cell raises at parse
    time, not deep in a running session. Since #3376 P3 every cell of the
    current presentation x transport product is registered, so what this
    rejects today is a name that is not on either axis at all (a typo, or a
    presentation reyn does not have); it starts rejecting real combinations
    again the moment either axis gains a value."""
    if raw is None:
        return ToolUseConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"tool_use must be a mapping, got {type(raw).__name__}")

    if "chat" in raw:
        raise ValueError(
            "tool_use.chat is removed (FP-0066 P4b, #3247) — it has been "
            "split into tool_use.scheme (the presentation axis: "
            "'category' / 'enumerate-all' / 'retrieval', default "
            "'enumerate-all') and tool_use.transport (how the model "
            "expresses actions: 'tool_calls' / 'content_fence', default "
            "'tool_calls'). A former `chat: codeact` becomes "
            "`scheme: enumerate-all` + `transport: content_fence`. Update "
            "reyn.yaml — there is no compat alias for the old key."
        )

    def _name(key: str, default: str) -> str:
        if key not in raw:
            return default
        val = raw[key]
        if not isinstance(val, str) or not val:
            raise ValueError(
                f"tool_use.{key} must be a non-empty name, got {val!r}"
            )
        return val

    scheme = _name("scheme", "enumerate-all")  # #1657: owner default switch (H1 fix)
    transport_name = _name("transport", "tool_calls")

    from reyn.tools.transport import Transport, resolve_scheme_for_transport

    try:
        transport = Transport(transport_name)
    except ValueError:
        valid = ", ".join(t.value for t in Transport)
        raise ValueError(
            f"tool_use.transport must be one of [{valid}], got {transport_name!r}"
        ) from None

    # P4a's valid-pair registry is the live parse-time validation authority
    # (firm §2 J1): raises ValueError on an unregistered (scheme, transport)
    # cell, e.g. ('no-such-presentation', 'tool_calls'). The example is taken
    # from OUTSIDE the presentation axis on purpose — every example drawn from
    # inside it has gone false as the arc registered that cell (#3376 P2 and P3
    # each falsified the previous one).
    resolve_scheme_for_transport(scheme, transport)

    universal_wrappers_enabled = True
    if "universal_wrappers_enabled" in raw:
        val = raw["universal_wrappers_enabled"]
        if not isinstance(val, bool):
            raise ValueError(
                "tool_use.universal_wrappers_enabled must be a bool, "
                f"got {type(val).__name__}"
            )
        universal_wrappers_enabled = val

    return ToolUseConfig(
        scheme=scheme, transport=transport_name,
        universal_wrappers_enabled=universal_wrappers_enabled,
    )


