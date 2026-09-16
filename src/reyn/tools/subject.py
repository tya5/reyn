"""Resolve a tool call's declared "主役" (subject) value — #6184 段3-2.

The ONE producer-side reader of :attr:`~reyn.tools.types.ToolDefinition.
subject_params` (段3-1) — see that field's own docstring for what the
field means and why it is a pointer at the tool's OWN already-declared
parameter names, never new content. This module is the resolver; it
does not decide WHICH tools declare a subject (that lives on each
``ToolDefinition`` itself, one at a time, starting with ``exec``).

Lives in ``reyn/tools/`` (lead-coder ruling, #6184 issuecomment-
5685519787) — the same package ``subject_params`` and its own gate
(``check_subject_params_exist_in_own_schema.py``) live in, NOT
``core/present/`` (#6184 段2b-2's own home): a ``reyn.tools`` import from
there would be a genuinely NEW layer edge (confirmed unused today,
lead-coder's own grep), where ``runtime → tools`` already exists
(``registry.py:5988``, ``router_loop.py:43,61,1254``) — this module lets
``lifecycle_forwarder.py`` (this stage's one producer call site) reuse
that EXISTING edge instead of opening a new one.
"""
from __future__ import annotations


def resolve_tool_subject(tool_name: str, args: object) -> object:
    """The declared subject's RAW value for one tool call, or ``None``.

    #6184 BLOCKING (lead-coder, measured, PR #6200 review): this used to
    return ``str(args[param])`` — for ``exec``'s own ``argv`` (a
    ``list[str]``), that stringifies to a Python repr
    (``"['python', '-m', 'pytest']"``), the EXACT display shape the
    owner's original request asked to move away from ("エグゼクであれば
    実行されたコマンド列自体を表示の中心にしたい"). The architect's own
    段3 design (issuecomment-5685059570) puts the value→display-string
    conversion in ``core/present/tool_call_compose.py`` (str → as-is,
    ``list[str]`` → space-joined so ``argv`` reads as a command line,
    anything else → the existing normalize path) — a raw ``list`` value
    stringified HERE, before that conversion ever runs, would have
    already discarded the list structure the join needs. This function
    now returns the raw value UNCONVERTED; see
    :func:`~reyn.core.present.tool_call_compose.render_subject` for the
    conversion, called by this stage's one producer call site
    (``lifecycle_forwarder._enqueue_tool_call``) AFTER this function.

    ``None`` when: the tool is not registered, declares no
    ``subject_params`` (every tool but ``exec`` today — 段3-1's own
    accept ④, additive and inert), ``args`` is not a dict, or none of
    the declared param names are PRESENT in ``args`` (checked by
    membership, never truthiness — the same discipline #6184 段2b-3's
    own ``details["args"]`` read already established: an explicitly
    empty string is still a DECLARED value, distinct from "this call
    never passed that argument at all"). Checked in the tool's own
    declared PRIORITY order — the first param name present wins, even
    if a later one in the tuple is also present.

    Registry lookup is a fresh, per-call :func:`~reyn.tools.
    get_default_registry` + :meth:`~reyn.tools.registry.ToolRegistry.
    lookup` — the SAME "per-call lookup is cheap enough, do not invent a
    second cache" shape already established at
    ``reyn.runtime.router_tools.get_dispatch_kind`` (this module's own
    style precedent) and confirmed cheap even on a live repaint path by
    ``gutter.py``'s own ``_is_retrieval_tool`` (verbatim: "Kept cheap —
    ``decorate`` runs on every repaint"); THIS call site runs once per
    tool-call lifecycle event, strictly less often.
    """
    if not isinstance(args, dict):
        return None
    from reyn.tools import get_default_registry

    definition = get_default_registry().lookup(tool_name)
    if definition is None or not definition.subject_params:
        return None
    for param in definition.subject_params:
        if param in args and args[param] is not None:
            return args[param]
    return None


def subject_param_names(tool_name: str) -> "tuple[str, ...]":
    """A tool's own declared ``subject_params`` tuple, or ``()`` if the
    tool is unregistered or declares none — #6184 段3-3's one CONSUMER-side
    reader (the display side's own need: which of a message's own
    ``details["args"]`` keys were already shown as the subject, so they
    can be excluded from the ``k=v`` listing rather than shown twice —
    accept ⑵, architect issuecomment-5689016062).

    Deliberately NOT re-deriving the resolved subject VALUE (that stays
    :func:`resolve_tool_subject`'s own producer-side job, called once,
    at ``lifecycle_forwarder._enqueue_tool_call`` — see that function's
    own docstring for why the RAW value is resolved there and not
    re-derived here) — this returns only the declared NAMES, letting a
    consumer match them against its own already-composed ``(key,
    value)`` pairs by KEY, never re-running the producer's own
    priority-order value scan a second time on the display path.

    Same "fresh per-call registry lookup, no second cache" shape this
    module's own :func:`resolve_tool_subject` and
    ``gutter.py:_is_retrieval_tool`` already established."""
    from reyn.tools import get_default_registry

    definition = get_default_registry().lookup(tool_name)
    if definition is None:
        return ()
    return definition.subject_params
