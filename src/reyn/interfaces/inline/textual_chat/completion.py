"""Inline completion for the composer's ``/`` and ``:`` namespaces (#3354).

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

``:`` triggers only when the token STARTS the input or FOLLOWS WHITESPACE, and
only once at least :data:`SKILL_MIN_CHARS` characters follow the colon. Both
gates are required, not either/or: a colon is far too common in ordinary prose
to trigger on, and the word-boundary rule alone still fires on ``note: x`` while
the length rule alone still fires on ``http://xx``. Together they keep
``http://x``, ``12:30``, ``ratio:2`` and ``note: see below`` quiet.

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

from textual.widgets import OptionList

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

#: How many characters must follow ``:`` before the skill menu opens. See the
#: module docstring's trigger rules for why this is required ALONGSIDE the
#: word-boundary gate rather than instead of it.
SKILL_MIN_CHARS = 2

#: The LAST ``:name`` token of the line. Three constraints, all load-bearing:
#: it must start the input or follow whitespace (``(?:^|\s)`` — never mid-word,
#: so ``http://x`` stays quiet), it must carry at least :data:`SKILL_MIN_CHARS`
#: name characters, and nothing may follow it (``$`` — once a space follows a
#: resolved ``:name`` the user is past completing IT; a further stacked
#: ``:name2`` matches on its own).
_SKILL_TOKEN_RE = re.compile(rf"(?:^|\s):([A-Za-z0-9_-]{{{SKILL_MIN_CHARS},}})$")

#: Shown as the sole row when a namespace IS readable but nothing matched. The
#: menu deliberately stays OPEN in that case: a silent close is
#: indistinguishable from "no menu was ever triggered", which is the worse
#: outcome for discoverability — the user cannot tell a typo'd command from a
#: completion feature that is not working.
NO_MATCH_ROW = "no matches"

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
    cmd: "SlashCommand", arg_partial: str, session: object,
) -> "tuple[CompletionCandidate, ...]":
    """Run ``cmd``'s ``CompleterFn`` and prefix-filter its result.

    Honours the declared signature verbatim — ``completer(session,
    arg_partial)`` — so a completer that reads the partial (``/image``'s path
    walker, ``/memory``'s ``view`` sub-command gate) gets it.

    Any exception is swallowed to an empty tuple: a broken completer must not
    break typing (the contract ``_image_path_completer`` documents on its own
    side). That yields a no-match menu rather than silence, which is right — the
    namespace DID trigger and the command DOES declare a completer; it simply
    produced nothing this time.
    """
    try:
        values = cmd.completer(session, arg_partial)  # type: ignore[misc]
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


def _argument_state(text: str, session: object, registry) -> CompletionState:
    """The ``/cmd <arg>`` branch: the command word is settled, so command-name
    candidates STOP and the command's own ``CompleterFn`` takes over.

    Opens for either of two independent reasons — the command has an argument
    source here (a ``CompleterFn`` AND a local session to call it with), or it
    declares a ``usage`` line to show. The second is what makes the stage useful
    for the 15 commands that document their syntax and offer no completer
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
    has_source = cmd.completer is not None and session is not None
    if not has_source and not header:
        return NO_COMPLETION
    word = _completing_word(arg_partial)
    return CompletionState(
        kind=KIND_ARGUMENT,
        candidates=(
            _argument_candidates(cmd, arg_partial, session) if has_source else ()
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
    session: object = None,
    skills: "Sequence[SkillEntry] | None" = None,
    registry=None,
) -> CompletionState:
    """The completion state for ``text`` (the composer's text BEFORE the cursor).

    Pure: every candidate comes from a supplied source (the slash registry, a
    command's ``CompleterFn``, the skill entry list), none is synthesised here.

    ``registry`` defaults to the process-wide slash ``REGISTRY`` — always
    available, which is why ``/`` COMMAND-name completion works on every client
    including a remote one.

    ``session`` is the local ``Session`` a ``CompleterFn`` is called with.
    ``None`` means "this client has no session", and argument completion stays
    SILENT rather than calling every completer with ``None`` and rendering their
    uniformly-empty results as a no-match menu.

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
            return _argument_state(text, session, registry)
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
    match = _SKILL_TOKEN_RE.search(text)
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


class CompletionPopup(OptionList, can_focus=False):
    """The candidate menu, drawn directly above the input row.

    A NON-FOCUSABLE :class:`~textual.widgets.OptionList` — the drawer picker
    panes' existing widget vocabulary, and Textual's own command-palette idiom
    (``CommandList(OptionList, can_focus=False)``). Measured: a non-focusable
    ``OptionList`` receives no key events, so focus stays on the composer and
    this widget only ever re-renders. ``chrome.Composer`` drives it through
    :meth:`sync` and reads :attr:`owns_keys` before claiming a navigation key.
    """

    DEFAULT_CSS = """
    CompletionPopup {
        display: none;
        height: auto;
        max-height: 10;
        background: $panel;
        border: none;
        padding: 0;
    }
    """

    def on_mount(self) -> None:
        self.display = False
        self._state: CompletionState = NO_COMPLETION
        # The (kind, token_start) identity of the token an Esc dismissed, or
        # None when nothing is dismissed. See :meth:`sync` for the re-arm rule.
        self._dismissed: "tuple[str, int] | None" = None

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
            self.clear_options()
            # The SHARED ``Content``-literal wrap, never a re-derived one: an
            # ``OptionList`` markup-parses a bare ``str`` (#3302), and ``/image``
            # candidates are filesystem names.
            self.add_options(option_content_rows(rows))
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
        content the widget never actually mounted."""
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
    "CompletionCandidate",
    "CompletionPopup",
    "CompletionState",
    "compute_completion",
]
