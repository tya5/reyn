r"""Inline completion for the composer's ``/`` and ``:`` namespaces (#3354).

The retired prompt_toolkit inline app completed both namespaces in its input
box (``_SlashCompleter`` / ``_SkillInvokeCompleter``, merged into one
``PromptSession`` completer); the Textual rebuild (#3273 Phase 6) deleted that
app and never re-wired the UI. The DATA side survived untouched, so this module
supplies **no completion logic of its own** — it calls the same sources the
retired app and the dispatch path already use:

- :func:`~reyn.interfaces.slash.slash_command_completions` — ``/`` command names.
- ``SlashCommand.completer`` (the per-command ``CompleterFn``,
  ``(session, arg_partial="") -> list[str]``) — ``/cmd <arg>`` arguments.
- :func:`~reyn.interfaces.skill_invoke.skill_invoke_completions` — ``:`` skill
  names, fed the SAME ``SkillEntry`` list ``Session._maybe_handle_skill_invoke``
  enforces its ``menu``/``on_demand``/``hidden`` surface from
  (:meth:`~reyn.runtime.session.Session.available_skills`), so a `hidden` skill
  can never be suggested here while being un-invocable there.

Two halves, split so the interesting one needs no widget mounted:

- :func:`compute_completion` — PURE. Text-before-cursor in, a
  :class:`CompletionState` out. It owns the trigger + routing rules below.
- :class:`CompletionPopup` — a NON-FOCUSABLE
  :class:`~textual.widgets.OptionList`, the same list-with-selection widget the
  bottom-chrome drawer's picker panes already use. Measured: a non-focusable
  ``OptionList`` receives no key events, so focus never leaves the composer and
  ``chrome.Composer._on_key`` remains the SINGLE owner of every keystroke —
  which is what keeps the key contract in one place instead of split across two
  focus targets. (This is also Textual's own command-palette idiom,
  ``textual.command.CommandList(OptionList, can_focus=False)``.)

Trigger rules
-------------
``/`` triggers only at the START of the input (a slash command is inherently
line-initial) and completes the COMMAND NAME until the first space; from the
space on the command word is settled and the menu switches to that command's
ARGUMENT completions — ``/model `` stops offering ``/matrix``/``/memory`` and
starts offering model classes. A leading ``/`` owns the whole line, so
``/answer :x`` is read as ``/answer``'s argument, never as a skill token.

The argument stage also carries the command's :attr:`~reyn.interfaces.slash.
SlashCommand.usage` line as a non-selectable HEADER row (#3364) — see
:func:`_usage_header`.

``:`` triggers only when the token STARTS the input or FOLLOWS WHITESPACE — the
word-boundary gate, and the one that carries the prose-quieting weight. Measured
against the four counterexamples this section used to name (#3541): ``http://x``,
``12:30``, ``ratio:2`` and ``note: see below`` are ALL rejected by the boundary
rule alone, because every one of them has a non-space character immediately
before the colon (in ``note:`` the colon follows ``e``, so neither ``^`` nor
``\s`` matches). The section previously claimed the boundary rule alone "still
fires on ``note: x``" and that the length rule alone "still fires on
``http://xx``"; direct evaluation falsifies both — the first because of the ``e``,
the second because ``//`` is not in the name character class.

A LENGTH gate of :data:`SKILL_MIN_CHARS` characters after the colon therefore
survives only MID-LINE, and only for the case the boundary rule genuinely cannot
see: a colon that really does start a word inside prose, i.e. a trailing ``word
:`` or ``see :`` where the user is punctuating rather than invoking. No measured
threat is on record for it; it is kept because mid-line is where an unwanted
menu costs the most and offers the least.

At LINE START there is no such case — no counterexample has anything before the
colon to be ambiguous about — so ``^:`` fires with ZERO characters typed and
lists every available skill, exactly as ``/`` does (#3541: the asymmetry was an
owner-reported bug, not a design). ``:``, ``:a`` and ``:ab`` all open the menu;
``hello :`` and ``hello :a`` stay quiet while ``hello :ab`` opens it.

Two separate patterns express this rather than one two-alternative regex, so
each keeps its name in group **1** — ``compute_completion`` derives
``prefix_len`` from ``len(match.group(1))`` and ``token_start`` from
``match.start(1)``, and a shifted group number would corrupt the accepted text
silently instead of raising.

A newline anywhere disables completion entirely — neither namespace is
multi-line (the rule both retired completers applied).

Source availability vs. no matches
----------------------------------
These are DIFFERENT states and the UI must not conflate them. A namespace whose
SOURCE this client cannot read stays completely silent (``kind`` is
:data:`KIND_NONE`, nothing renders); a namespace that IS readable but matched
nothing shows the menu with an explicit :data:`NO_MATCH_ROW`. A remote
``--connect`` client holds no ``Session``, so argument CANDIDATES and skill
completion are silent there rather than rendering an empty-looking menu that
would read as "no such command exists" — see :func:`compute_completion`'s
``session`` / ``skills`` parameters, where ``None`` means "source unavailable"
and an empty sequence means "source readable, nothing in it". The line is drawn
on the SOURCE, not the stage: everything REGISTRY-derived — command names, and
the argument stage's usage header — works on every client.

:attr:`CompletionState.has_candidate_source` carries that same distinction into
the STATE, because a usage header can now open a menu with no candidate source
behind it at all (``/compact `` — a real command with a documented usage line
and no ``CompleterFn``). Such a menu shows the header ALONE: appending
:data:`NO_MATCH_ROW` there would claim the user's argument matched nothing when
nothing was ever offered to match against.

Row height
----------
Every row is ONE line, clipped to the width with :data:`ROW_ELLIPSIS`
(:func:`clipped_rows`). A skill description is long enough to wrap at any
realistic terminal width, and the height that wrapping consumes comes out of how
many CANDIDATES are visible: measured at 80 columns with three skills installed,
the rows ran 5, 7 and 4 lines, so one of three fit the ten-line menu (#3551).
The menu's job is choosing which skill; the description that matters is the one
read after choosing — the owner's ruling, against showing more of one
description.

Clipping is measured in CELLS (``rich.cells``), the measure Textual's compositor
uses, so a description containing wide characters clips where the terminal
actually runs out of columns. Getting that wrong does not merely misalign: an
overflowing row is re-wrapped by Textual itself, at column 0, which is the #3545
defect — a row reading as two candidates — arriving through the back door.

The width is not known until layout, so :meth:`CompletionPopup.on_resize`
re-clips — see that method for why the first ``sync`` alone is not enough.

An earlier report also read as text being LOST mid-word, and there are now TWO
cuts in front of a reader — they are not the same cut and only one of them is
recoverable by widening the terminal. ``skills.entries.<name>.description`` is
capped at LOAD (``reyn.data.skills.registry._truncate_description``, on a word
boundary, ellipsised); this module then clips what survives to the CURRENT
WIDTH. Both end in :data:`ROW_ELLIPSIS`, so the glyph means "there is more"
either way — which is the honest reading, since a reader who wants the rest
opens the skill rather than resizing to find it.

**Security.** Candidate rows AND the usage header reach the terminal through
:func:`~reyn.interfaces.inline.textual_chat.presenter._neutralized_label` and the
shared ``Content``-literal wrap
(:func:`~reyn.interfaces.inline.textual_chat.presenter.option_content_rows`) —
never a bare ``str`` handed to ``OptionList``, which markup-parses it (#3302).
Command and skill names are operator/config-derived, but ``/image``'s
``CompleterFn`` returns FILESYSTEM names — an attacker-chosen filename is
exactly the untrusted-text case that boundary exists for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len, set_cell_size
from textual.widgets import OptionList

from reyn.interfaces import palette

from .presenter import _neutralized_label, option_content_rows

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reyn.data.skills.registry import SkillEntry
    from reyn.interfaces.slash import SlashCommand

#: Completion namespaces — the ``kind`` discriminator of :class:`CompletionState`.
#: A typed marker rather than a sniffed string: the popup, the composer's accept
#: path and the tests all branch on it. :data:`KIND_NONE` means NOT TRIGGERED
#: (render nothing) — distinct from a triggered namespace with zero matches.
KIND_COMMAND = "command"
KIND_ARGUMENT = "argument"
KIND_SKILL = "skill"
KIND_NONE = ""

#: How many characters must follow a MID-LINE ``:`` before the skill menu opens.
#: Does NOT apply at line start (#3541) — see the module docstring's trigger
#: rules, which record that the word-boundary gate alone already rejects every
#: counterexample on file, so this is a prose guard for the un-measured trailing
#: ``word :`` case, not the rule keeping ``http://x``/``12:30`` quiet.
SKILL_MIN_CHARS = 2

#: The LINE-INITIAL ``:name`` token. No length floor: nothing precedes the colon,
#: so there is no prose case to be ambiguous with, and a bare ``:`` opens the
#: full skill list the way a bare ``/`` opens the full command list. Nothing may
#: follow the name (``$`` — once a space follows a resolved ``:name`` the user is
#: past completing IT).
_SKILL_TOKEN_LINE_START_RE = re.compile(r"^:([A-Za-z0-9_-]*)$")

#: The LAST ``:name`` token when it follows WHITESPACE mid-line. Same anchoring,
#: plus the :data:`SKILL_MIN_CHARS` floor so a trailing ``word :`` in prose does
#: not open a menu. Kept as its OWN pattern rather than a second alternative in
#: the line-start one so that the name stays group 1 in both — see the module
#: docstring's closing note on ``prefix_len``/``token_start``.
_SKILL_TOKEN_MIDLINE_RE = re.compile(
    rf"\s:([A-Za-z0-9_-]{{{SKILL_MIN_CHARS},}})$"
)

#: Shown as the sole row when a namespace IS readable but nothing matched. The
#: menu deliberately stays OPEN in that case: a silent close is
#: indistinguishable from "no menu was ever triggered", which is the worse
#: outcome for discoverability — the user cannot tell a typo'd command from a
#: completion feature that is not working.
NO_MATCH_ROW = "no matches"

#: What a clipped row ends with (#3551). U+2026, matching the cap
#: ``reyn.data.skills.registry`` applies at load — one glyph means "there is
#: more", wherever the reader meets it.
#:
#: East Asian **Ambiguous** width: ``rich.cells.cell_len`` resolves it to 1 and
#: so does every terminal reyn has been checked on, but a terminal configured to
#: treat ambiguous characters as wide would draw it in two columns and overflow
#: the row by one. ``gutter.py`` carries the same caveat for the same reason, so
#: the constant is measured rather than assumed to be one column.
ROW_ELLIPSIS = "…"
_ELLIPSIS_CELLS = cell_len(ROW_ELLIPSIS)

#: A newline with any whitespace around it — folded to a single space before a
#: row is measured (see :func:`clipped_rows`).
_NEWLINE_RUN_RE = re.compile(r"[ \t]*\n[ \t]*")

#: Prefix of the argument-stage usage header row (#3364). The ``↳`` chrome and
#: the ``usage:`` word are the POPUP's, not the command's — ``SlashCommand.usage``
#: holds the bare syntax line (``/copy [N|list]``) and every command spells it
#: the same way, so the label cannot drift per command.
USAGE_ROW_PREFIX = "↳ usage: "


@dataclass(frozen=True)
class CompletionCandidate:
    """One offered completion.

    ``value`` is the token text that REPLACES the typed prefix on accept (no
    sigil — the ``/`` or ``:`` the user already typed stays put); ``label`` is
    what the row displays (sigil included, so a ``/`` row reads ``/model``);
    ``detail`` is the command summary / skill description, ``""`` when the
    source has none to give.
    """

    value: str
    label: str
    detail: str = ""

    def row(self) -> str:
        """This candidate's display row, neutralized at the display boundary
        (see the module docstring's security note)."""
        label = _neutralized_label(self.label)
        if not self.detail:
            return label
        return f"{label}  {_neutralized_label(self.detail)}"


@dataclass(frozen=True)
class CompletionState:
    """What the popup should be showing for one text-before-cursor.

    ``kind`` is the TRIGGER discriminator: :data:`KIND_NONE` means no namespace
    fired and nothing renders. Any other kind means the menu is open — even with
    zero ``candidates``, which renders :data:`NO_MATCH_ROW`.

    ``prefix_len`` is how many characters immediately before the cursor the
    typed prefix occupies — accepting replaces exactly those, so the sigil and
    everything left of the token survive untouched. ``token_start`` is where
    that token begins in the text this state was computed from; together with
    ``kind`` it forms the token IDENTITY a sticky dismissal is keyed on (see
    :meth:`CompletionPopup.sync`).

    ``accept_suffix`` is appended after the accepted value: a space for the two
    NAME namespaces (the token is finished, and for ``/`` that trailing space is
    exactly what advances the menu to the argument stage), but empty for an
    ARGUMENT — a ``/image`` directory row must stay navigable rather than be
    terminated.

    ``header`` is an INFORMATIONAL first row that is not a candidate: it renders
    above the candidates and can never be accepted (see :attr:`row_offset`).
    ``has_candidate_source`` says whether anything was asked for candidates at
    all — see the module docstring's "Source availability vs. no matches".
    """

    kind: str = KIND_NONE
    candidates: "tuple[CompletionCandidate, ...]" = ()
    prefix_len: int = 0
    token_start: int = -1
    accept_suffix: str = ""
    header: str = ""
    has_candidate_source: bool = True

    @property
    def is_open(self) -> bool:
        """Whether the menu is showing — i.e. whether a namespace TRIGGERED.
        The VISIBILITY predicate; :attr:`owns_keys` is the key-interception one
        and they differ only for a usage-header-only menu."""
        return self.kind != KIND_NONE

    @property
    def owns_keys(self) -> bool:
        """Whether the menu should CLAIM ``↑``/``↓``/``Tab``/``Esc``, as opposed
        to merely being visible.

        These come apart exactly once: a usage-header-only menu (#3364), which
        has no candidate source behind it and therefore nothing to navigate,
        accept or ever have. It is a hint, not a picker — so ``Tab`` keeps its
        #3277 composer→MenuBar meaning and ``↑`` keeps reaching the sent-queue
        while it is up. Claiming them would eat both keys for the WHOLE time the
        user types ``/visibility on tool foo``, with no effect to show for it.

        A no-match menu still owns them: a real source WAS asked, the row set can
        change on the next keystroke, and having Tab move focus out from under a
        menu the user is actively narrowing is the surprise that rule exists to
        prevent."""
        return self.is_open and (bool(self.candidates) or self.has_candidate_source)

    @property
    def identity(self) -> "tuple[str, int]":
        """``(kind, token_start)`` — the token this state completes. Two states
        share an identity while the user edits the SAME token."""
        return (self.kind, self.token_start)

    @property
    def row_offset(self) -> int:
        """How many leading display rows are NOT candidates — ``1`` while a
        ``header`` is shown, else ``0``.

        The single place the header's presence turns into an index shift, so
        :meth:`CompletionPopup.selected` and
        :meth:`CompletionPopup.move_selection` cannot disagree about it. Without
        it, ``highlighted == 0`` would mean "the header" on screen and "the first
        candidate" to the accept path — Tab would insert a candidate the user
        never highlighted.
        """
        return 1 if (self.is_open and self.header) else 0

    def rows(self) -> "list[str]":
        """The display rows: the optional header, then the candidate rows — or
        the single no-match row when a REAL candidate source triggered and
        matched nothing (see the module docstring's availability distinction).
        """
        if not self.is_open:
            return []
        out = [_neutralized_label(self.header)] if self.header else []
        if self.candidates:
            out.extend(c.row() for c in self.candidates)
        elif self.has_candidate_source:
            out.append(NO_MATCH_ROW)
        return out


#: The "no namespace triggered" state — shared, immutable.
NO_COMPLETION = CompletionState()


def _completing_word(arg_partial: str) -> str:
    """The word an argument completion is completing: the LAST whitespace-
    delimited token of ``arg_partial``, or ``""`` when the partial ends on a
    space (nothing typed yet → offer everything).

    Multi-word argument lines are why this is the last word and not the whole
    partial: ``/memory view fo`` must filter by ``fo``, not by ``view fo`` — the
    earlier words are context for CHOOSING the candidates (``/memory``'s
    completer only answers for the ``view`` sub-command), not part of the prefix
    being matched or replaced.
    """
    return "" if arg_partial.endswith(" ") else arg_partial.rsplit(" ", 1)[-1]


def _argument_candidates(
    cmd: "SlashCommand", arg_partial: str, source: object,
) -> "tuple[CompletionCandidate, ...]":
    """Run ``cmd``'s ``CompleterFn`` and prefix-filter its result.

    Honours the declared signature verbatim — ``completer(source,
    arg_partial)`` — so a completer that reads the partial (``/image``'s path
    walker, ``/memory``'s ``view`` sub-command gate) gets it. ``source`` is a
    ``CompletionSourceSnapshot | None`` (#5044) — a plain value, never a live
    ``Session`` — see that class's own docstring.

    Any exception is swallowed to an empty tuple: a broken completer must not
    break typing (the contract ``_image_path_completer`` documents on its own
    side). That yields a no-match menu rather than silence, which is right — the
    namespace DID trigger and the command DOES declare a completer; it simply
    produced nothing this time.
    """
    try:
        values = cmd.completer(source, arg_partial)  # type: ignore[misc]
    except Exception:  # noqa: BLE001 — a completer must never break the composer
        return ()
    word = _completing_word(arg_partial)
    return tuple(
        CompletionCandidate(value=str(v), label=str(v))
        for v in (values or [])
        if str(v).startswith(word)
    )


def _usage_header(cmd: "SlashCommand") -> str:
    """``↳ usage: <cmd.usage>``, or ``""`` for a command that declares none.

    ``SlashCommand.usage`` was added FOR this row — its own comment names "what
    shows once the user types ``/<cmd> ``" — but the surface it named (the
    retired ``SlashPicker``) was deleted by the #3273 Phase 6 rebuild and the
    replacement popup (#3354) never read the field (#3364).

    The ARGUMENT stage is where it goes, not the command-name list. Two reasons,
    both measured rather than assumed: 20 of the 25 non-hidden commands set
    ``usage``, so a per-candidate second line would double the height of almost
    every row and halve how many commands fit under the popup's 10-row cap — the
    geometry #3358 measured at 80×24; and the argument stage is the only moment
    the answer is ACTIONABLE, since by then the user has committed to the command
    and is typing the very arguments the line describes.

    Empty for the 5 non-hidden commands with no ``usage`` (``/agents``,
    ``/cost``, ``/list``, ``/quit``, ``/exit`` — all of them argument-less) — the
    header is omitted entirely rather than reserved as a blank or placeholder
    row.
    """
    return f"{USAGE_ROW_PREFIX}{cmd.usage}" if cmd.usage else ""


def _argument_state(text: str, source: object, registry) -> CompletionState:
    """The ``/cmd <arg>`` branch: the command word is settled, so command-name
    candidates STOP and the command's own ``CompleterFn`` takes over.

    Opens for either of two independent reasons — the command has an argument
    source here (a ``CompleterFn`` AND a ``CompletionSourceSnapshot`` to call
    it with, #5044 — never a live ``Session``), or it declares a ``usage``
    line to show. The second is what makes the stage useful for the 15
    commands that document their syntax and offer no completer
    (``/visibility ``, ``/hook ``, ``/session ``…): before #3364 those opened
    nothing at all. Only 5 commands have a ``CompleterFn``, so the usage line is
    what the argument stage has to offer three times out of four.

    Stays SILENT for an unrecognised command, and for a command with neither an
    argument source nor a usage line — an empty menu would read as "no such
    command exists".
    """
    cmd_name, _, arg_partial = text[1:].partition(" ")
    cmd = registry.get(cmd_name)
    if cmd is None:
        return NO_COMPLETION
    header = _usage_header(cmd)
    has_source = cmd.completer is not None and source is not None
    if not has_source and not header:
        return NO_COMPLETION
    word = _completing_word(arg_partial)
    return CompletionState(
        kind=KIND_ARGUMENT,
        candidates=(
            _argument_candidates(cmd, arg_partial, source) if has_source else ()
        ),
        prefix_len=len(word),
        token_start=len(text) - len(word),
        accept_suffix="",
        header=header,
        has_candidate_source=has_source,
    )


def compute_completion(
    text: str,
    *,
    source: object = None,
    skills: "Sequence[SkillEntry] | None" = None,
    registry=None,
) -> CompletionState:
    """The completion state for ``text`` (the composer's text BEFORE the cursor).

    Pure: every candidate comes from a supplied source (the slash registry, a
    command's ``CompleterFn``, the skill entry list), none is synthesised here.

    ``registry`` defaults to the process-wide slash ``REGISTRY`` — always
    available, which is why ``/`` COMMAND-name completion works on every client
    including a remote one.

    ``source`` is a ``reyn.interfaces.repl.read_model.CompletionSourceSnapshot
    | None`` (#5044, architect ruling — see that class's own docstring) a
    ``CompleterFn`` is called with. ``None`` means "this client has no
    session", and argument completion stays SILENT rather than calling every
    completer with ``None`` and rendering their uniformly-empty results as a
    no-match menu.

    ``skills`` follows the same convention, and the distinction is load-bearing:
    ``None`` = the skill source is unavailable (stay silent), an empty sequence =
    the source is readable and simply has nothing (open a no-match menu).

    See the module docstring for the trigger rules.
    """
    if registry is None:
        from reyn.interfaces.slash import REGISTRY

        registry = REGISTRY
    if "\n" in text:
        return NO_COMPLETION
    if text.startswith("/"):
        if " " in text:
            return _argument_state(text, source, registry)
        from reyn.interfaces.slash import slash_command_completions

        prefix = text[1:]
        pairs = slash_command_completions(prefix, commands=registry.all_commands())
        return CompletionState(
            kind=KIND_COMMAND,
            candidates=tuple(
                CompletionCandidate(value=name, label=f"/{name}", detail=summary)
                for name, summary in pairs
            ),
            prefix_len=len(prefix),
            token_start=1,
            accept_suffix=" ",
        )
    if skills is None:
        return NO_COMPLETION
    # Line start first: it is the strictly more permissive rule, and the two
    # patterns cannot both match the same text (one requires ``^:``, the other a
    # whitespace before the colon).
    match = _SKILL_TOKEN_LINE_START_RE.match(text) or _SKILL_TOKEN_MIDLINE_RE.search(
        text
    )
    if match is None:
        return NO_COMPLETION
    from reyn.interfaces.skill_invoke import skill_invoke_completions

    prefix = match.group(1)
    pairs = skill_invoke_completions(prefix, list(skills))
    return CompletionState(
        kind=KIND_SKILL,
        candidates=tuple(
            CompletionCandidate(value=name, label=f":{name}", detail=description)
            for name, description in pairs
        ),
        prefix_len=len(prefix),
        token_start=match.start(1),
        accept_suffix=" ",
    )


def clipped_rows(rows: "Sequence[str]", width: int) -> "list[str]":
    """``rows`` clipped to ONE line of ``width`` columns each, with
    :data:`ROW_ELLIPSIS` where text was dropped (#3551, owner ruling A).

    The menu's job is choosing WHICH skill; the description that matters is the
    one read after choosing. Wrapping spent the height on one candidate's prose:
    measured at 80 columns with three skills installed, the rows ran 5, 7 and 4
    lines, so one of three fit the ten-line menu and the third was entirely off
    screen. At one line each, ten fit.

    Measured in CELLS, not characters. ``rich.cells.set_cell_size`` is the same
    measure Textual's compositor uses, so a description containing wide
    characters clips where the terminal actually runs out of columns — ``len``
    would leave a CJK row overflowing by its own width again and Textual would
    re-wrap the overflow at column 0, which is #3545 back. A wide character
    straddling the boundary is replaced by a space by that helper rather than
    half-drawn.

    Each row stays ONE option, as before: the highlight,
    :attr:`CompletionState.row_offset` and :meth:`CompletionPopup.selected` all
    index OPTIONS, so a candidate must never become two.

    Returns ``rows`` untouched when ``width`` is not yet known (a widget that
    has never been laid out reports ``0``) or is too narrow to hold the ellipsis
    plus a character — the caller re-runs this on ``Resize``, and until then
    Textual's own wrap is a strictly better fallback than a crash or a
    one-character column.
    """
    if width <= _ELLIPSIS_CELLS + 1:
        return list(rows)
    out: "list[str]" = []
    for row in rows:
        # Rows arrive single-line; a newline would still be a second visual line
        # after clipping, so fold NEWLINES before measuring rather than after.
        # Only newlines: the two spaces between a row's label and its detail are
        # the column separator ``_labels``-style readers split on, so collapsing
        # runs of spaces would quietly destroy the row's structure.
        flat = _NEWLINE_RUN_RE.sub(" ", row)
        if cell_len(flat) <= width:
            out.append(flat)
            continue
        out.append(set_cell_size(flat, width - _ELLIPSIS_CELLS) + ROW_ELLIPSIS)
    return out


class CompletionPopup(OptionList, can_focus=False):
    """The candidate menu, drawn directly above the input row.

    A NON-FOCUSABLE :class:`~textual.widgets.OptionList` — the drawer picker
    panes' existing widget vocabulary, and Textual's own command-palette idiom
    (``CommandList(OptionList, can_focus=False)``). Measured: a non-focusable
    ``OptionList`` receives no key events, so focus stays on the composer and
    this widget only ever re-renders. ``chrome.Composer`` drives it through
    :meth:`sync` and reads :attr:`owns_keys` before claiming a navigation key.
    """

    DEFAULT_CSS = palette.css("""
    CompletionPopup {
        display: none;
        height: auto;
        max-height: 10;
        background: @surface@;
        border: none;
        padding: 0;
    }
    """)

    def on_mount(self) -> None:
        self.display = False
        self._state: CompletionState = NO_COMPLETION
        # The (kind, token_start) identity of the token an Esc dismissed, or
        # None when nothing is dismissed. See :meth:`sync` for the re-arm rule.
        self._dismissed: "tuple[str, int] | None" = None
        # The width the mounted options were wrapped for, or -1 when nothing is
        # mounted. Read by :meth:`on_resize` to skip the rebuild when the width
        # did not actually change — without it, a rebuild that alters this
        # auto-height widget's height re-enters Resize and never settles.
        self._wrapped_at: int = -1

    def sync(self, state: CompletionState) -> None:
        """Show ``state``, unless it completes a token the user already dismissed.

        **Sticky dismissal.** After ``Esc``, typing MORE of the same token must
        not silently reopen the menu — the user said no to this completion, and a
        menu that springs back on the next keystroke ignores that. The dismissal
        is released only by a FRESH TRIGGER: a state whose
        :attr:`CompletionState.identity` differs from the dismissed one (a new
        ``/``/``:`` token, or the same command advancing from its NAME stage to
        its ARGUMENT stage), or the trigger lapsing entirely.
        """
        if self._dismissed is not None:
            if not state.is_open:
                self._dismissed = None
            elif state.identity == self._dismissed:
                return
            else:
                self._dismissed = None
        rows = state.rows()
        changed = rows != self._state.rows()
        self._state = state
        self.display = state.is_open
        if changed:
            self._mount_rows(rows)
            # A new row set always re-seats the highlight on its first CANDIDATE
            # row (past the usage header, when there is one): a stale index would
            # point at a row from the previous keystroke's list, and a highlight
            # on the header would offer an un-acceptable row as the Tab target.
            # A header-only menu highlights NOTHING — it is a hint, and drawing
            # it as the selected row of a list would invite a Tab that cannot do
            # anything.
            if state.candidates:
                self.highlighted = state.row_offset
            elif self.option_count and state.has_candidate_source:
                self.highlighted = 0
            else:
                self.highlighted = None

    def _wrap_width(self) -> int:
        """The column count a row may occupy — the scrollable content region, so
        the vertical scrollbar a >10-row menu grows is already subtracted. One
        column too many and Textual re-wraps the overflow itself, at column 0,
        which is the defect back again on exactly the long menus that need the
        indent most."""
        return self.scrollable_content_region.width

    def _mount_rows(self, rows: "list[str]") -> None:
        """Wrap ``rows`` for the current width and mount them as the options.

        The SHARED ``Content``-literal wrap, never a re-derived one: an
        ``OptionList`` markup-parses a bare ``str`` (#3302), and ``/image``
        candidates are filesystem names.
        """
        width = self._wrap_width()
        self.clear_options()
        self.add_options(option_content_rows(clipped_rows(rows, width)))
        self._wrapped_at = width

    def on_resize(self) -> None:
        """Re-wrap the mounted rows when the width changes under them.

        The FIRST :meth:`sync` of a session runs while this widget is still
        ``display: none`` and therefore zero-wide, so the wrap it performs is
        the no-op fallback; the layout that follows delivers the real width
        here. Without this the indent would never appear at all on the first
        menu, and a terminal resize would leave a menu wrapped for the old
        width until the next keystroke happened to change the row set.
        """
        if self._state.is_open and self._wrap_width() != self._wrapped_at:
            keep = self.highlighted
            self._mount_rows(self._state.rows())
            self.highlighted = keep

    def dismiss_current(self) -> None:
        """Close the menu and remember the token it was showing, so typing more
        of that same token does not reopen it (the ``Esc`` path)."""
        self._dismissed = self._state.identity if self._state.is_open else None
        self._reset()

    def close(self) -> None:
        """Close the menu with NO dismissal memory — the accept path and the
        post-submit / restore resets, none of which is the user saying no, so
        completion re-arms immediately."""
        self._dismissed = None
        self._reset()

    def _reset(self) -> None:
        self._state = NO_COMPLETION
        self.clear_options()
        self._wrapped_at = -1
        self.display = False

    @property
    def is_open(self) -> bool:
        """Whether the menu is showing. True for a no-match menu too (see
        :attr:`CompletionState.is_open`)."""
        return self._state.is_open

    @property
    def owns_keys(self) -> bool:
        """Whether the menu claims the navigation keys — the predicate the
        composer gates its key interception on. Narrower than :attr:`is_open`
        by exactly the usage-header-only menu (see
        :attr:`CompletionState.owns_keys`)."""
        return self._state.owns_keys

    def state(self) -> CompletionState:
        """The state currently displayed — the public read the composer's accept
        path uses for ``prefix_len``/``accept_suffix``, and a test uses to
        inspect candidates without touching private attributes."""
        return self._state

    def selected(self) -> "CompletionCandidate | None":
        """The highlighted candidate, or ``None`` when the menu is closed, is
        showing the no-match row, or is showing only a usage header (there is
        nothing to accept in any of those).

        The display index is translated back through
        :attr:`CompletionState.row_offset`, so a header row highlighted by a
        mouse click resolves to ``None`` rather than to candidate 0."""
        index = self.highlighted
        if not self._state.candidates or index is None:
            return None
        position = index - self._state.row_offset
        if not 0 <= position < len(self._state.candidates):
            return None
        return self._state.candidates[position]

    def move_selection(self, delta: int) -> None:
        """Move the highlight by ``delta`` rows, CLAMPED at both ends.

        No wrap-around: the sent-queue's ``↑``/``↓`` and the drawer's
        ``OptionList`` panes both clamp, and a wrapping menu would make ``↑``
        from the first row jump to the bottom rather than feel like the edge it
        is. The TOP clamp sits below the usage header when there is one, so ``↑``
        stops at the first candidate instead of parking on a row Tab cannot
        accept."""
        if not self._state.candidates:
            return
        offset = self._state.row_offset
        current = self.highlighted
        if current is None:
            current = offset
        self.highlighted = max(
            offset, min(current + delta, offset + len(self._state.candidates) - 1)
        )

    def rendered_rows(self) -> "list[str]":
        """The rows currently displayed — the public read a test asserts
        displayed content through (mirrors ``SentQueue.rendered_texts``). Taken
        off the MOUNTED options rather than recomputed, so it cannot claim
        content the widget never actually mounted.

        One entry per OPTION, and — since #3551 — one visual line per entry:
        :func:`clipped_rows` returns each row already cut to the width, so a
        test reads the row exactly as the terminal draws it, including the
        trailing :data:`ROW_ELLIPSIS` when text was dropped."""
        return [
            str(self.get_option_at_index(i).prompt)
            for i in range(self.option_count)
        ]


__all__ = [
    "KIND_ARGUMENT",
    "KIND_COMMAND",
    "KIND_NONE",
    "KIND_SKILL",
    "NO_COMPLETION",
    "NO_MATCH_ROW",
    "SKILL_MIN_CHARS",
    "USAGE_ROW_PREFIX",
    "ROW_ELLIPSIS",
    "CompletionCandidate",
    "CompletionPopup",
    "CompletionState",
    "compute_completion",
    "clipped_rows",
]
