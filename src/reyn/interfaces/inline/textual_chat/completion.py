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
``--connect`` client holds no ``Session``, so argument and skill completion are
silent there rather than rendering an empty-looking menu that would read as "no
such command exists" — see :func:`compute_completion`'s ``session`` / ``skills``
parameters, where ``None`` means "source unavailable" and an empty sequence
means "source readable, nothing in it".

**Security.** Candidate rows reach the terminal through
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
    """

    kind: str = KIND_NONE
    candidates: "tuple[CompletionCandidate, ...]" = ()
    prefix_len: int = 0
    token_start: int = -1
    accept_suffix: str = ""

    @property
    def is_open(self) -> bool:
        """Whether the menu is showing — i.e. whether a namespace TRIGGERED.
        The predicate the composer gates its key interception on, so a no-match
        menu still owns ``↑``/``↓``/``Tab``/``Esc``: the arrows go to whatever
        menu is visible, without the user needing to know whether it happens to
        have rows right now."""
        return self.kind != KIND_NONE

    @property
    def identity(self) -> "tuple[str, int]":
        """``(kind, token_start)`` — the token this state completes. Two states
        share an identity while the user edits the SAME token."""
        return (self.kind, self.token_start)

    def rows(self) -> "list[str]":
        """The display rows: candidate rows, or the single no-match row when the
        namespace triggered but matched nothing."""
        if not self.is_open:
            return []
        if not self.candidates:
            return [NO_MATCH_ROW]
        return [c.row() for c in self.candidates]


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


def _argument_state(text: str, session: object, registry) -> CompletionState:
    """The ``/cmd <arg>`` branch: the command word is settled, so command-name
    candidates STOP and the command's own ``CompleterFn`` takes over.

    Stays SILENT (not a no-match menu) whenever this client has no argument
    source at all: an unrecognised command, a command declaring no completer, or
    no local session to call one with. Only a command that really can be
    completed here opens a menu.
    """
    cmd_name, _, arg_partial = text[1:].partition(" ")
    cmd = registry.get(cmd_name)
    if cmd is None or cmd.completer is None or session is None:
        return NO_COMPLETION
    word = _completing_word(arg_partial)
    return CompletionState(
        kind=KIND_ARGUMENT,
        candidates=_argument_candidates(cmd, arg_partial, session),
        prefix_len=len(word),
        token_start=len(text) - len(word),
        accept_suffix="",
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
    :meth:`sync` and reads :attr:`is_open` before claiming a navigation key.
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
            # A new row set always highlights its first row: a stale highlight
            # index would point at a row from the previous keystroke's list.
            if self.option_count:
                self.highlighted = 0

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
        """Whether the menu is showing — the predicate the composer gates its
        key interception on. True for a no-match menu too (see
        :attr:`CompletionState.is_open`)."""
        return self._state.is_open

    def state(self) -> CompletionState:
        """The state currently displayed — the public read the composer's accept
        path uses for ``prefix_len``/``accept_suffix``, and a test uses to
        inspect candidates without touching private attributes."""
        return self._state

    def selected(self) -> "CompletionCandidate | None":
        """The highlighted candidate, or ``None`` when the menu is closed or
        showing the no-match row (there is nothing to accept)."""
        index = self.highlighted
        if not self._state.candidates or index is None:
            return None
        if not 0 <= index < len(self._state.candidates):
            return None
        return self._state.candidates[index]

    def move_selection(self, delta: int) -> None:
        """Move the highlight by ``delta`` rows, CLAMPED at both ends.

        No wrap-around: the sent-queue's ``↑``/``↓`` and the drawer's
        ``OptionList`` panes both clamp, and a wrapping menu would make ``↑``
        from the first row jump to the bottom rather than feel like the edge it
        is."""
        if not self._state.candidates:
            return
        current = self.highlighted or 0
        self.highlighted = max(
            0, min(current + delta, len(self._state.candidates) - 1)
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
    "CompletionCandidate",
    "CompletionPopup",
    "CompletionState",
    "compute_completion",
]
