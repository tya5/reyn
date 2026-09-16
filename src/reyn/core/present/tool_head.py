"""The single PUBLIC compose point for a tool-call row's head line
(everything AFTER the bold tool name) — #6184 段3-3.

## The bug this closes — #6193

Three CONSUMER sites build this line: ``presenter.py``'s ``_tool_head``
and ``_collapsed_retrieval_line``, and ``renderer.py``'s
``format_inline_message`` (``tool_call_started`` branch). #6193's own
population (architect, issuecomment-5689016062, corrected from #6193's
original 2-site body — a 3rd site had been found by 段2b-3's own census
but never fed back): none of the 3 ever routed this line through
:func:`~reyn.core.present.guard.get_neutralizer` at all — an ESC/control
byte in a tool arg reached the terminal raw.

## Single boundary — #4758's own discipline, reapplied here

Architect's own ruling (issuecomment-5689016062, verbatim): "tool head
の表示文字列を返す口が1つ在り、その戻り値の境界で1度だけ中和／surface
は呼び手が渡す" — a naive per-site fix (adding a ``get_neutralizer(...)``
call at each of the 3 sites separately) would repeat #4758's own named
failure mode verbatim ("remembering at every FUTURE one"). This module
is THAT one mouth: every one of the 3 sites calls
:func:`compose_tool_head` instead of building the line by hand, and
this function's own body contains the ONLY ``get_neutralizer(`` call in
the whole path (grep-checkable — #6184 accept ⑥, this stage's own
gate-shaped witness lives in this arc's test file, not a CI script,
since the population is exactly 3 named call sites, not an open-ended
one).

``surface`` is caller-supplied, never hardcoded here (the same "surface
is a consumer/viewer property, not something a shared compose function
may assume" principle this arc has applied since 段2b-2's own
``compose_tool_call_text`` — producer never guesses a surface; here the
function is consumer-SIDE, but still does not decide FOR the caller
which surface it is rendering to).

## Layering — why this does NOT import ``reyn.tools``

A tool's own declared ``subject_params`` (which ``details["args"]`` keys
were already shown as the subject, and so must be excluded from the
``k=v`` listing — accept ⑵) is NOT looked up in here. ``core/present/``
does not import ``reyn.tools`` today (``tool_call_compose.py``'s own
docstring: a ``reyn.tools`` import from THIS package would be a
genuinely NEW layer edge, confirmed unused, deliberately avoided by
段3-2's own producer-side design for the same reason). Each of the 3
CALLERS (already living in ``interfaces/``, where an ``interfaces ->
tools`` edge is already established — ``gutter.py:_is_retrieval_tool``)
resolves the tool's own declared names via
:func:`~reyn.tools.subject.subject_param_names` and passes the result in
as ``subject_keys`` — a plain ``frozenset[str]``, not a registry
handle — so this module stays reyn.tools-free.

## Truncation — reimplemented, not imported

The value/total-width cut below duplicates
``renderer.py``'s own private ``_cut`` byte-for-byte rather than
importing it: ``core/present/`` sits BELOW ``interfaces/`` in this
codebase's layering (``interfaces`` already imports FROM
``core/present/guard.py``; the reverse edge does not exist and must
not be opened for a single truncate helper). This mirrors
``tool_call_compose.py``'s own precedent exactly (that module's
docstring: "This module reimplements the whitespace-collapse half
independently instead" of importing ``renderer.py``'s
``_normalize_text``) — the same boundary, the same choice, made twice
now for the same reason.

## Subject stays UNCUT — accept ④

Only the ``k=v`` listing passes through the width cut below; the
``subject`` half is placed into the final string as-is, never
shortened. At width overflow, the trailing OPTION is what disappears,
never the middle of the subject — the owner's own original ask (the
executed command line as the display's CENTER) would be defeated by a
subject that could itself be cut mid-string.

## No empty parens — #6205 (a 段3-2/3-3/3-4 regression)

``args_display`` is ``""`` — not ``"()"`` — when there are no args left
to show (either the tool genuinely takes none, or ``subject_keys``
excluded its one and only param). #6184 段3-2/3-3/3-4 gave `subject`
this exclusion mechanism, which OPENED this hole: a single-param tool
whose one param became the subject (``read_file``'s own ``path``) now
composes ZERO remaining pairs, and the pre-#6205 code still wrapped
that empty joined string in ``()`` unconditionally — ``read_file
docs/x.md ()``, a trailing empty pair the reader has no reason to
trust means anything.

ONE rule, not a `"drop the parens only when subject is set"` special
case (lead-coder's own ruling, #6205: a special case closes this ONE
hole and reopens on the next path that reaches zero args a different
way) — a tool that has never taken an arg (``list_plugins``) ALSO now
composes with no parens at all (``list_plugins``, not
``list_plugins()``) — accepted on purpose (issue #6205: "行頭に tool
名が在る時点でそれは既に分かります", the parens were never the ONLY
signal this is a call).
"""
from __future__ import annotations

from reyn.core.present.guard import get_neutralizer


def _cut(s: str, n: int) -> str:
    """Byte-identical to ``renderer.py``'s own private ``_cut`` — see
    this module's own docstring for why it is duplicated here rather
    than imported."""
    return s if len(s) <= n else s[: n - 1] + "…"


def compose_tool_head(
    subject: "str | None",
    composed_args: "list[tuple[str, str]] | str",
    *,
    subject_keys: "frozenset[str]" = frozenset(),
    surface: str = "terminal",
    value_width: int = 24,
    total_width: int = 60,
) -> "tuple[str, str]":
    """Returns ``(subject_display, args_display)`` — ``args_display`` is
    the parenthesized ``"(k=v, ...)"`` string when at least one pair (or
    a non-empty bare value) remains, and ``""`` — never ``"()"`` — when
    none do (#6205: a caller must NOT special-case "empty parens only
    when there is no subject"; this function's own empty-means-empty
    rule already covers a tool that never took an arg the same way it
    covers one whose only arg became the subject). ``subject_display``
    is ``""`` when ``subject`` is ``None`` (a caller omits both the text
    and its own leading separator in that case — accept ③'s
    byte-identity depends on the caller doing so, not on this function
    inserting a placeholder).

    ``composed_args`` is whatever :func:`~reyn.interfaces.repl.renderer.
    _compose_args` (or the producer's own ``details["args"]``, #6184
    段2b-3) already produced — a list of ``(key, value)`` pairs, or a
    bare normalized string. Pairs whose key is a member of
    ``subject_keys`` are excluded from the listing (accept ⑵: the value
    already shown as ``subject`` must not ALSO appear in ``k=v``, or it
    is shown twice) — membership only, no value comparison (the
    rendered ``subject`` string and the same value's own composed form
    are NOT guaranteed to match textually, e.g. ``exec``'s ``argv``
    renders space-joined as the subject but reprs as a list in its own
    composed arg value — key identity is the only reliable signal).

    The per-value cut runs BEFORE the join, then the whole joined line
    is cut again to ``total_width`` — so one long value cannot consume
    another key's budget (#6207: this is the same two-stage discipline
    ``renderer.py``'s own retired ``_truncate_args`` used to state and
    execute for the pre-3-3 call path; this function is now the one
    place it runs).

    ONE call to :func:`~reyn.core.present.guard.get_neutralizer`,
    passed ``surface`` — the single syntactic call site this whole path
    has (#6193 accept ⑥) — even though
    ``.neutralize()`` itself runs on each half separately so a caller
    can style ``subject``/args differently without re-deriving
    neutralization on its own (accept ⑤: an ESC byte in EITHER half is
    stripped, since both halves go through the SAME neutralizer
    instance).

    Byte-identical to the pre-3-3 (段2b) ``args_display`` when the input
    carries no control/ESC byte (accept ③): ``neutralize`` is a true
    no-op on clean text (:func:`~reyn.core.present.guard.
    strip_control_chars` only strips the C0/C1/DEL ranges, never an
    ordinary printable character).
    """
    neutralizer = get_neutralizer(surface)

    if isinstance(composed_args, list):
        pairs = [(k, v) for k, v in composed_args if k not in subject_keys]
        joined = ", ".join(f"{k}={_cut(v, value_width)}" for k, v in pairs)
        args_str = _cut(joined, total_width) if pairs else ""
    elif composed_args:
        args_str = _cut(composed_args, total_width)
    else:
        args_str = ""
    # #6205: empty parens are never drawn — ONE rule, no "only when a
    # subject is present" special case (that would just move the hole,
    # not close it — see this function's own module docstring's #6205
    # section). `args_str` empty means NOTHING follows, not `"()"`.
    args_display = ""
    if args_str:
        args_display, _ = neutralizer.neutralize(f"({args_str})")

    subject_display = ""
    if subject:
        subject_display, _ = neutralizer.neutralize(subject)

    return subject_display, args_display
