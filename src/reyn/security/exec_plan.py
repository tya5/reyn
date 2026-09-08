"""``ExecPlan`` — the parser front-end for #5838 (owner decision: "shell
文字列を受け、解析した実行計画にポリシーを掛け、実行は sandbox 内の
`sh -c` に元の文字列を渡す。LLM の exec・pipeline の exec step・
`/exec`・`!`/`!!` すべて同じ front-end").

This module is 段2 of #5838's implementation plan (architect,
issuecomment-5557210786): the ONE parser every caller shares. It does
NOT check policy and does NOT execute anything — 段3 (segment policy:
tool axis + threat scan per segment's resolved argv[0], redirect targets
through the existing ``require_file_write``/``require_file_read``) and
段4 (``sh -c`` execution of the ORIGINAL string, never a reconstruction
from this parse) are separate, later stages. This module's only job:
turn a shell-looking command line into a policy-checkable
:data:`ExecPlan`, or reject it outright when it cannot be safely
decomposed.

Placed here (not ``interfaces/``, per architect's explicit ruling: "LLM
経路も呼ぶ") because the LLM ``exec`` tool path (``tools/exec.py``,
段4/6) and the operator ``/exec``/``!``/``!!`` path
(``interfaces/slash/exec.py``, 段7) both need the SAME parser — one
function, one place, matching #5837's own isolation of
``tokenize_exec_cmdline`` this module replaces.

## Why "解析して policy、実行は元の文字列" (parse for policy, execute
the ORIGINAL string) rather than executing this module's own parsed
segments

Owner picked option (i) — matching how Claude Code / Codex / OpenClaw /
Hermes all actually work (architect's competitive research,
``docs/deep-dives/research/competitive/shell-interpretation.md``):
policy reads the PARSED plan, but the shell that actually runs gets the
ORIGINAL string, unparsed. architect's own (ii) proposal (execute the
parsed segments directly via the backend, never start a real shell) was
explicitly NOT chosen — "私の提案でしたが競合に無い一歩なので採りません"
— because the owner's request was to move CLOSER to what competitors
do, and (ii) would have been a step none of them take. The known cost of
(i) (real audit-event trace will show this once 段4/5 land): a
parse/execute semantic gap can exist — the exact class of two real
Codex incidents architect's research names (a `./sed` basename bypass,
a zsh-fork sandbox-wrapper bypass). This parser's rejection list below
exists specifically to keep that gap as narrow as competitors' own.

## Denylist -> allowlist (architect's structural ruling, PR #5984
BLOCKING, after 4 independent bypasses surfaced in ~40 minutes of
review: ``$``/glob/``~``, brace ``{a,b}``, a literal newline, ``!``)

This module's FIRST version enumerated dangerous characters/constructs
and rejected each one it found (a denylist). That denylist did not
converge — a THIRD PARTY's shell (`/bin/sh`, whichever binary that
resolves to on whatever machine this runs) decides what is special, and
that population is not something this module's own source code can
read and enumerate exhaustively; each review pass found one more
character this parser had never considered. A denylist ALSO fails in
the dangerous direction on anything it has not yet enumerated (unknown
-> accepted), the opposite of what a front-end whose only job is
"policy's reading matches the shell's reading" needs (unknown -> must
be refused).

This module now does the reverse: :data:`_ALLOWED_RAW_CHARS` is an
ALLOWLIST checked against the RAW input string, before any tokenizing
— not the per-token denylist checks the first version used (a
per-token check misses a literal newline entirely: ``shlex`` folds it
into ordinary whitespace before this module ever sees a token). Any
character not in the allowed set — known dangerous or not yet
discovered — is refused. See :func:`_reject_disallowed_raw_characters`
for the set itself and the per-character justification (never "looks
harmless" — "unquoted, ``shlex``'s posix tokenizer and ``sh`` agree on
what this character means").

## What this parser accepts

- The allowed literal characters (:data:`_ALLOWED_RAW_CHARS`) —
  letters, digits, and a short list of punctuation ``shlex`` and ``sh``
  read identically when unquoted (see that constant's own table).
- ``shlex``-quoted words — single/double quotes, backslash escapes;
  the SAME primitive #5837's own (now-superseded) ``tokenize_exec_
  cmdline`` used for the no-shell argv form. Quoting lets a word
  contain a SPACE (already allowed raw) but does NOT let it contain a
  character outside the allowlist — quoting is not an escape hatch
  around the allowlist here (see :func:`_reject_disallowed_raw_
  characters`'s own docstring for why the check runs on raw text,
  quote-position-blind).
- Chain operators: ``|`` (pipe), ``&&`` (AND), ``||`` (OR), ``;``
  (sequence).
- Redirects: ``>`` (truncate), ``>>`` (append), ``<`` (input) — each
  followed by exactly one path token, and each must be the LAST thing
  in its segment (no argv token after a redirect's path — see
  :class:`ExecPlanRejected`'s own docstring for why this is a
  deliberate v1 narrowing, not an oversight).

## What this parser rejects

Any character outside the allowlist above (this is now the primary
defense, not an enumerated list — see the denylist/allowlist section),
PLUS two things a character allowlist cannot express because they are
about POSITION and CONTEXT, not individual characters:

- A leading ``NAME=value`` environment-assignment prefix — every
  character in ``FOO=bar`` is individually allowed (letters, ``=``),
  but in the LEADING position of a segment it would become that
  segment's ``argv[0]``, hiding the REAL command from tool-axis policy
  entirely (``FOO=bar rm -rf /tmp/x`` → this parser's ``argv[0]`` would
  be ``"FOO=bar"``, never ``"rm"`` — the same basename-bypass class
  Codex #28732 named, reached through assignment instead of a relative
  path). A LATER argument shaped like ``NAME=value`` (``docker run -e
  FOO=bar image``) is unaffected.
- A QUOTED token whose dequoted value happens to consist entirely of
  operator characters (e.g. ``grep '|' file``) — string-identical to a
  real unquoted operator once ``shlex`` strips the quotes, so this
  parser cannot verify which the shell would really do; see
  :func:`_iter_tokens`'s own docstring for how this is detected and why
  it is refused rather than guessed either way.
- Malformed operator/redirect placement (an empty segment, a redirect
  with no target, an argument after a redirect's target, an
  unrecognised punctuation run) or an unterminated quote (``shlex``
  itself raises for the last one).

Rejection is LOUD (a raised exception with a human-readable reason),
never a silent pass-through — matching Claude Code's "解析不能なら
プロンプトへ" / Codex's ``used_complex_parsing`` escalation / OpenClaw's
"chain/redirect は全 segment が通らない限り拒否" (architect's own
competitive summary): every competitor's shape is "refuse, don't guess."

## If your command gets rejected

This parser accepts a narrower set of commands than a real shell does
— a real, benign command using a character outside the allowlist (a
literal ``$``, a glob, brace expansion, home-directory ``~``, or a
character this module has not yet allowlisted) is refused rather than
guessed at. Two ways forward, once #5838's later stages land: (1) the
no-shell argv form (#5837 — quote nothing, no operators, exactly the
literal words to run) stays available wherever this parser's caller
also offers it; (2) if you genuinely need one of these characters in a
shell command, that character can be added to :data:`_ALLOWED_RAW_CHARS`
with the SAME justification every existing entry carries ("unquoted,
shlex and sh agree") — file it rather than working around this parser.
"""
from __future__ import annotations

import io
import re
import shlex
from dataclasses import dataclass
from typing import cast

# The operator characters this parser recognises — the ONE source of
# truth for both the raw-string allowlist (_ALLOWED_RAW_CHARS, below)
# and shlex's own tokenizing (_iter_tokens passes this as
# punctuation_chars). A token whose dequoted VALUE is entirely made of
# these characters is either a real operator (unquoted) or, if it was
# quoted, rejected outright (_iter_tokens's own docstring — this parser
# cannot tell the two apart by value alone, and refuses to guess).
_OPERATOR_CHARS = "|&;><"
_PUNCTUATION_CHARS = _OPERATOR_CHARS

# Single-character operator tokens this parser accepts standalone.
_CHAIN_OPS_SINGLE = frozenset({"|", ";"})
_REDIRECT_OPS_SINGLE = frozenset({">", "<"})
# Two-character operator tokens -- shlex itself already emits these as
# ONE token when the two characters are adjacent in *text* with no
# whitespace between them (punctuation_chars mode groups a run of
# consecutive punctuation characters; see _iter_tokens's own
# docstring), so no merging happens in this module. "&&"/"||"/">>" are
# the only doubled forms #5838's plan supports; "<<" is heredoc,
# rejected explicitly in the main parse loop, never treated as a usable
# operator here.
_CHAIN_OPS_DOUBLE = frozenset({"&&", "||"})
_REDIRECT_OPS_DOUBLE = frozenset({">>"})

# #5838 BLOCKING (architect's structural ruling, issuecomment-5578494687,
# after 4 independent bypasses in ~40 minutes of denylist review: $/
# glob/~, brace {a,b}, a literal newline, ! negation) -- see this
# module's own docstring, "Denylist -> allowlist," for why this replaced
# a per-character denylist. Every character below is admitted for the
# SAME reason (architect's own required criterion, never "looks
# harmless"): unquoted, shlex's posix tokenizer and sh's own
# interpretation agree on what this character means.
#
# | chars              | why unquoted shlex and sh agree                |
# |--------------------|-------------------------------------------------|
# | A-Z a-z 0-9         | ordinary word characters to both; no operator/  |
# |                     | expansion meaning either side                    |
# | ``-``               | option-flag marker to both; not a shell          |
# |                     | metacharacter to either                          |
# | ``_``               | ordinary identifier/word character to both       |
# | ``.``               | literal in both -- NOT a glob metacharacter by   |
# |                     | itself (only ``* ? [`` are)                      |
# | ``/``               | path separator, literal to both                  |
# | ``,``               | literal to both UNLESS paired with ``{``/``}``   |
# |                     | (brace expansion) -- those two are NOT in this   |
# |                     | set, so a lone ``,`` has no special sh meaning   |
# | ``:``               | literal to both -- PATH-list-separator is a      |
# |                     | convention programs read, not shell syntax       |
# | ``=``               | ordinary word character to both -- its only      |
# |                     | special meaning is POSITIONAL (a leading         |
# |                     | ``NAME=`` prefix), a separate check below, not a |
# |                     | per-character concern                            |
# | ``+``               | literal to both                                  |
# | ``@``               | literal to both for non-interactive ``sh -c``    |
# | ``%``               | literal to both for non-interactive ``sh -c``    |
# |                     | (job-control ``%`` is an interactive-shell-only  |
# |                     | feature, not reachable through this front-end)   |
# | ``#``               | agree even though it IS special to both: shlex's |
# |                     | own default `commenters` is `#` (rest-of-line    |
# |                     | ignored), matching `sh`'s own comment syntax     |
# |                     | exactly -- `ls # rm -rf /` reads identically on  |
# |                     | both sides (architect's own confirmed-safe case) |
#
# Quote characters (`'`/`"`) let a WORD contain a space (already
# allowed raw) but do not admit any character outside this set --
# quoting is not an escape hatch around the allowlist (module docstring,
# "What this parser accepts"). Whitespace (space/tab) separates words;
# a NEWLINE is deliberately excluded (module docstring's own newline
# section) -- `shlex` folds it into ordinary whitespace, but a real
# shell reads it as a command separator. Recognised operators
# (_OPERATOR_CHARS) are admitted as themselves, checked for a supported
# shape by the main parse loop below (an unrecognised punctuation run,
# e.g. `;;`, still raises there).
_LITERAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "-_./,:=+@%#"
)
_QUOTE_CHARS = frozenset("'\"")
_RAW_WHITESPACE_CHARS = frozenset(" \t")
_ALLOWED_RAW_CHARS = (
    _LITERAL_CHARS | _QUOTE_CHARS | frozenset(_OPERATOR_CHARS) | _RAW_WHITESPACE_CHARS
)


def _reject_disallowed_raw_characters(text: str) -> None:
    """The primary defense (architect's structural ruling, replacing a
    per-character denylist — see this module's own docstring): raises
    :class:`ExecPlanRejected` if *text* contains ANY character outside
    :data:`_ALLOWED_RAW_CHARS`, checked against the RAW string, before
    any tokenizing.

    Raw, not per-token, on purpose: a per-token check cannot see a
    NEWLINE at all — ``shlex`` folds it into ordinary whitespace before
    ever producing a token, which is exactly how the newline bypass
    (module docstring) reached this module's own denylist-era version.
    Quote-position-blind, also on purpose: this does not attempt to
    special-case a disallowed character that happens to sit inside a
    quoted region — the SAME "cannot verify, so refuse" judgement
    :func:`_iter_tokens` already applies to a quoted operator-shaped
    token, applied here to every other character too, not a second,
    narrower rule."""
    disallowed = sorted(set(text) - _ALLOWED_RAW_CHARS)
    if disallowed:
        raise ExecPlanRejected(
            f"character(s) not supported here: {disallowed!r} — this "
            "parser only accepts a fixed allowlist of characters whose "
            "meaning is guaranteed to match between shlex and a real "
            "shell (see the module's own docstring, \"If your command "
            "gets rejected,\" for what to do next)"
        )


# #5838 BLOCKING: `FOO=bar rm -rf /tmp/x` -- a leading `NAME=value`
# environment-assignment prefix -- is not stripped by this parser, so
# `argv[0]` becomes the literal string `"FOO=bar"`, never `"rm"`. Any
# future tool-axis policy (段3) reading `argv[0]` would check the WRONG
# binary entirely; the real `rm` never reaches policy at all -- the
# exact `./sed` basename-bypass class architect's own competitive
# research (Codex #28732) names, reached through a different door. Not
# expressible as a character allowlist entry (every character in
# `FOO=bar` is individually allowed) -- this is a POSITIONAL check.
# Matched only in LEADING position (before the first real word of a
# segment — see `_looks_like_leading_assignment`'s own caller) so a
# legitimate later argument shaped like `NAME=value` (e.g. `docker run
# -e FOO=bar image`) is unaffected: `docker` already satisfied "a real
# word was seen" before `FOO=bar` appears.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _looks_like_leading_assignment(token: str) -> bool:
    """True if *token* has the ``NAME=value`` shape of a shell
    environment-assignment prefix — see ``_ASSIGNMENT_RE``'s own
    module-level comment for the measured bypass this closes. Callers
    apply this ONLY while still in a segment's leading position."""
    return bool(_ASSIGNMENT_RE.match(token))


class ExecPlanRejected(Exception):
    """Raised by :func:`parse_exec_plan` when *text* contains a
    character outside :data:`_ALLOWED_RAW_CHARS`, or a shell construct
    this parser cannot safely decompose into policy-checkable segments
    (#5838 段2) — see this module's own docstring, "What this parser
    rejects" and "Denylist -> allowlist," for the reasoning.

    This is a v1 narrowing, disclosed, not a claim of covering every
    legitimate shell command: a real, benign command using a character
    or construct this parser does not yet accept (a heredoc, a
    subshell, `2>` fd-numbered redirects, argv tokens after a redirect)
    is refused with a reason, the same "refuse, don't guess" shape
    every competitor #5838's own research found already takes — see the
    module docstring's own "If your command gets rejected" section for
    what to do next."""


@dataclass(frozen=True)
class ExecSegment:
    """One command in the plan — the SAME argv shape #5837's own
    (pre-#5838) ``tokenize_exec_cmdline`` returned, now one element of a
    larger :data:`ExecPlan` instead of the whole result. 段3's own
    segment-level policy (tool axis, threat scan) reads ``argv[0]``
    from here."""

    argv: "tuple[str, ...]"


@dataclass(frozen=True)
class ExecChainOp:
    """A chain operator between two segments — ``op`` is one of
    ``"|"``, ``"&&"``, ``"||"``, ``";"``."""

    op: str


@dataclass(frozen=True)
class ExecRedirect:
    """A redirect attached to the segment immediately before it in the
    plan — ``op`` is one of ``">"``, ``">>"``, ``"<"``. 段3's own policy
    routes ``path`` through the existing ``require_file_write``/
    ``require_file_read`` (the same file-axis primitives production
    already uses in 8 other places, per architect's own plan) — this
    module makes no permission decision of its own."""

    op: str
    path: str


#: One item of an :data:`ExecPlan` — a command, a chain operator, or a
#: redirect, in the SAME left-to-right order they appeared in the
#: original text (architect's own worked example: ``"ls -la | grep foo
#: > out.txt"`` -> ``[segment(["ls","-la"]), "|",
#: segment(["grep","foo"]), redirect(">", "out.txt")]``).
ExecPlanItem = ExecSegment | ExecChainOp | ExecRedirect

#: The full parsed plan for one command line — a flat, ordered list of
#: :data:`ExecPlanItem`. Never nested (a subshell/group would need
#: nesting, and #5838's plan rejects those constructs outright — see
#: this module's own docstring).
ExecPlan = list[ExecPlanItem]


def _iter_tokens(text: str) -> "list[tuple[str, bool]]":
    """Tokenize *text* with ``shlex`` (posix quoting rules, the SAME
    engine #5837's own ``tokenize_exec_cmdline`` used).

    Returns ``[(token, is_operator), ...]`` — ``is_operator=True`` means
    *token* is a run of one or more consecutive, UNQUOTED
    :data:`_PUNCTUATION_CHARS` characters. ``shlex`` itself already
    groups such a run into ONE token when ``punctuation_chars`` is set
    (e.g. ``"a && b"`` tokenizes to ``["a", "&&", "b"]`` directly, never
    ``["a", "&", "&", "b"]``) — this function does no merging of its
    own, only classifies what ``shlex`` already produced.

    #5838 BLOCKING (lead-coder co-vet, issuecomment-5578405655, real
    measurement): a QUOTED token whose dequoted VALUE happens to consist
    entirely of punctuation characters (e.g. ``grep '|' file``) is
    string-identical to a real unquoted operator once ``shlex`` strips
    the quotes — ``list(shlex.shlex(...))``'s public iterator throws
    that distinction away, and treating such a token as a literal word
    (the earlier, WRONG assumption this docstring used to state) let
    ``grep '|' file`` silently misparse into TWO piped commands instead
    of one ``grep`` call with a literal ``|`` argument. Tracked here
    instead by reading ``lexer.instream.tell()`` before/after each
    ``get_token()`` call to recover the RAW source substring for that
    token (``shlex``'s own scanner consumes a plain ``str`` through an
    internal ``io.StringIO``, whose ``tell()`` is character-position-
    accurate for this purpose) and checking whether that raw substring
    contains a quote character. A token that is BOTH punctuation-only
    AND was quoted raises :class:`ExecPlanRejected` outright — this
    parser cannot verify which the real shell would do (treat it as
    literal text, per the quoting, or — if some future construct this
    parser does not yet know about defeats the quote — as the operator
    it resembles) and refuses to guess, the SAME judgement
    :func:`_reject_disallowed_raw_characters` applies to every other
    character outside the allowlist.

    Raises :class:`ExecPlanRejected` for an unterminated quote (``shlex``
    itself raises ``ValueError``) or for a quoted operator-shaped token
    (above)."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCTUATION_CHARS)
    lexer.whitespace_split = True
    # shlex.shlex(str, ...) wraps a plain str argument in an io.StringIO
    # internally (stdlib source) -- typeshed's own instream type is a
    # narrower read-only Protocol that doesn't declare .tell(), so this
    # cast documents what is actually there rather than silencing an
    # unrelated mypy finding.
    instream = cast("io.StringIO", lexer.instream)
    operator_chars = frozenset(_PUNCTUATION_CHARS)
    tokens: "list[tuple[str, bool]]" = []
    try:
        while True:
            start = instream.tell()
            token = lexer.get_token()
            if token is None:
                break
            end = instream.tell()
            raw = text[start:end]
            quoted = "'" in raw or '"' in raw
            is_operator = bool(token) and all(c in operator_chars for c in token)
            if is_operator and quoted:
                raise ExecPlanRejected(
                    f"a quoted token that looks like an operator (token: "
                    f"{token!r}) is not supported here — this parser cannot "
                    "verify whether a real shell would treat it as literal "
                    "text or as the operator it resembles, and refuses to "
                    "guess"
                )
            tokens.append((token, is_operator))
    except ValueError as exc:
        raise ExecPlanRejected(f"could not parse arguments: {exc}") from exc
    return tokens


def parse_exec_plan(text: str) -> "ExecPlan":
    """The ONE parser #5838's plan uses everywhere (LLM ``exec`` tool,
    pipeline exec step, ``/exec``/``!``/``!!`` — see this module's own
    docstring). Turns *text* into an ordered list of
    :class:`ExecSegment`/:class:`ExecChainOp`/:class:`ExecRedirect`, or
    raises :class:`ExecPlanRejected` when *text* contains a construct
    this parser cannot safely decompose (see this module's own docstring
    for the full list).

    Never executes anything, never checks policy — purely a parse. 段4
    (execution) runs the ORIGINAL *text* through ``sh -c``, never a
    reconstruction from this function's return value (architect's own
    ruling: "実行は... 元の文字列を渡す" — see this module's docstring
    for why).

    The FIRST check (architect's structural ruling — see the module
    docstring's own "Denylist -> allowlist" section) is
    :func:`_reject_disallowed_raw_characters`, run against *text* BEFORE
    any tokenizing: this is what catches a literal newline (``shlex``
    folds it into ordinary whitespace, so a per-token check would never
    see it — ``"ls\\nrm -rf /tmp/x"`` would otherwise parse as ONE
    segment whose ``argv[0]`` is the ordinary, almost-certainly-allowed
    ``"ls"``, while a real shell runs the newline as a command
    SEPARATOR, running ``rm -rf /tmp/x`` as a second command policy
    never saw at all) and every other character this parser does not
    yet accept, in ONE place, by construction, rather than by
    enumerating each dangerous case as it is found."""
    _reject_disallowed_raw_characters(text)
    tokens = _iter_tokens(text)
    if not tokens:
        raise ExecPlanRejected("no command given")

    plan: "list[ExecPlanItem]" = []
    current_argv: "list[str]" = []
    redirect_closed_segment = False  # True once a redirect's path lands -- no more argv for this segment

    def _flush_segment(*, require_nonempty: bool, context: str) -> None:
        nonlocal current_argv, redirect_closed_segment
        if not current_argv:
            if require_nonempty:
                raise ExecPlanRejected(f"empty command {context}")
            return
        plan.append(ExecSegment(argv=tuple(current_argv)))
        current_argv = []
        redirect_closed_segment = False

    i = 0
    while i < len(tokens):
        token, is_op = tokens[i]
        if not is_op:
            if redirect_closed_segment:
                raise ExecPlanRejected(
                    "an argument cannot follow a redirect's target in this "
                    f"parser (token: {token!r}) — put every argument before "
                    "the redirect"
                )
            if not current_argv and _looks_like_leading_assignment(token):
                raise ExecPlanRejected(
                    f"a leading NAME=value environment assignment is not "
                    f"supported here (token: {token!r}) — it would become "
                    "this segment's argv[0], hiding the real command from "
                    "policy"
                )
            current_argv.append(token)
            i += 1
            continue

        if token in _CHAIN_OPS_SINGLE or token in _CHAIN_OPS_DOUBLE:
            _flush_segment(require_nonempty=True, context=f"before {token!r}")
            plan.append(ExecChainOp(op=token))
            i += 1
            continue

        if token in _REDIRECT_OPS_SINGLE or token in _REDIRECT_OPS_DOUBLE:
            if not current_argv and not (plan and isinstance(plan[-1], ExecRedirect)):
                raise ExecPlanRejected(f"redirect {token!r} with no preceding command")
            if current_argv:
                _flush_segment(require_nonempty=False, context=f"before {token!r}")
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is None or nxt[1]:
                raise ExecPlanRejected(f"redirect {token!r} has no target path")
            plan.append(ExecRedirect(op=token, path=nxt[0]))
            redirect_closed_segment = True
            i += 2
            continue

        if token.startswith("<<"):
            raise ExecPlanRejected(
                "heredoc (\"<<\") is not supported here — this parser only "
                "decomposes a command line into policy-checkable segments, "
                "and a heredoc's body is arbitrary multi-line content no "
                "segment-level check was designed to scan."
            )

        # A punctuation token that survived _iter_tokens but is not one
        # of #5838's supported operators/redirects -- a lone "&", or an
        # unrecognised run like "<>"/";;" (built entirely from allowed
        # operator characters, just not a recognised COMBINATION of
        # them -- "(", ")", "`" and everything else this branch used to
        # need to name are now unreachable, blocked earlier by
        # _reject_disallowed_raw_characters).
        raise ExecPlanRejected(
            f"unsupported shell construct (token: {token!r}) — this parser "
            "only supports pipes/chains (| && || ;) and redirects (> >> <), "
            "see the module docstring for the full rejection list"
        )

    _flush_segment(require_nonempty=not redirect_closed_segment, context="at end of input")
    if not plan or not any(isinstance(item, ExecSegment) for item in plan):
        raise ExecPlanRejected("no command given")
    return plan
