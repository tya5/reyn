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

## Denylist -> two-tier allowlist (architect's TWO rulings, PR #5984
BLOCKING — the second a self-correction of the first)

This module's FIRST version enumerated dangerous characters/constructs
and rejected each one it found (a denylist). That denylist did not
converge — a THIRD PARTY's shell (`/bin/sh`, whichever binary that
resolves to on whatever machine this runs) decides what is special, and
that population is not something this module's own source code can
read and enumerate exhaustively; 4 independent bypasses surfaced in
~40 minutes of review (`$`/glob/`~`, brace `{a,b}`, a literal newline,
`!` negation), each via a different probing approach.

The SECOND ruling (issuecomment-5578535394) corrected the first: a
raw-string-wide allowlist checked EVERY character the same way and
over-rejected real, benign commands (`python -c "print(1)"`,
`curl "http://x/a?b=1"`). Real-machine measurement found the actual
boundary was never "which characters are dangerous" — it was "which
characters does ``shlex``'s own ``punctuation_chars`` distinguish by
quoting":

- ``python -c "print(1)"`` -> ``['python','-c','print(1)']`` — QUOTED,
  ``shlex`` keeps the parens inside the WORD token, exactly like ``sh``
  would treat them (literal).
- ``python -c print(1)`` -> ``['python','-c','print','(','1',')']`` —
  UNQUOTED, ``shlex`` splits them into their OWN punctuation tokens.
- ``curl "...?b=1"`` and ``curl ...?b=1`` tokenize IDENTICALLY either
  way — ``?`` is not in ``shlex``'s ``punctuation_chars``, so quoting
  changes nothing observable, and this parser genuinely cannot recover
  which the caller meant.

So this module now runs TWO different checks, for two different
reasons, not one blanket allowlist:

1. :data:`_PUNCTUATION_CHARS` (`( ) | & ; < >` and backtick) — ``shlex``
   DOES distinguish quoted from unquoted for these, so they are checked
   per TOKEN, quote-aware: an unquoted occurrence becomes its own
   operator token (checked for a SUPPORTED shape below); a quoted one
   stays inside a word, accepted as literal text.
2. :data:`_ALWAYS_FORBIDDEN_CHARS` (`$ * ? [ ] { } ~ !`) — ``shlex``
   tokenizes these THE SAME WAY whether quoted or not, so there is no
   recoverable signal; every occurrence is rejected, quoted or not (see
   :func:`_reject_always_forbidden_characters`).

Only ONE check still runs against the RAW string before any tokenizing
(:func:`_reject_raw_newline`) — a literal newline, which never reaches
a per-token check at all: ``shlex`` folds it into ordinary whitespace
before producing a token.

## This is an approximation, not the industry's own unit (owner
directive "調べて" — architect's follow-up research, #5987)

A CHARACTER allowlist is not what competitors do. Claude Code / Codex
parse a real grammar (a tree-sitter AST) and allowlist NODE TYPES;
OpenClaw allowlists a resolved path + argument pattern over its own
execution plan. Both escalate what they cannot resolve (heredoc,
expansion) TO THE OPERATOR rather than refusing outright — architect's
own competitive research already recorded "人に回す" (hand it to a
human) for exactly this case, and this module does not do that (段3's
job, not this one — see :class:`ExecPlanRejected`'s own docstring). All
4 bypasses this module closes (a newline, `!`, `$`/glob/`~`, brace)
would show up structurally in a real AST — a separator node, a
negation node, an expansion node — not as a character to notice by
counting.

This module makes NO claim of completeness. Those 4 were each found by
real-machine MEASUREMENT (a live `sh -c` run compared against this
parser's own output), one probing pass at a time — there is no evidence
the enumeration in :data:`_ALWAYS_FORBIDDEN_CHARS`/
:data:`_PUNCTUATION_CHARS` is a copy of the true population of
characters/constructs a real shell treats specially, only that these 4
are covered. A 5th is not ruled out by anything this module's own
source can verify (see the module docstring's own denylist/allowlist
history above for why that is a structural limit of counting
characters, not a bug in THIS enumeration specifically).

``shlex`` has no grammar to allowlist nodes OF, so counting characters
was the only mechanism available here, not a considered design choice
matching the field's own unit. #5987 tracks re-evaluating the parser
itself (measured against real ``bash`` on one corpus, not "because it's
the standard" — the two obvious alternatives each have their own
measured failure modes: ``bashlex`` breaks on heredoc/arrays/arithmetic,
``tree-sitter-bash`` has carried real bugs in Codex's own use of it).
This module's own character-level approach stays as-is until that
lands.

## What this parser accepts

- Ordinary word characters — letters, digits, and punctuation neither
  :data:`_PUNCTUATION_CHARS` nor :data:`_ALWAYS_FORBIDDEN_CHARS` claims
  (e.g. ``- _ . / , : = + @ % #``) — literal to both ``shlex`` and
  ``sh`` in every position, quoted or not.
- ``shlex``-quoted words — single/double quotes, backslash escapes;
  the SAME primitive #5837's own (now-superseded) ``tokenize_exec_
  cmdline`` used for the no-shell argv form. Quoting DOES let a word
  contain one of :data:`_PUNCTUATION_CHARS`'s own characters as literal
  text (``"print(1)"``) — it does NOT rescue a character from
  :data:`_ALWAYS_FORBIDDEN_CHARS` (see the section above for why those
  two groups are treated differently).
- Chain operators: ``|`` (pipe), ``&&`` (AND), ``||`` (OR), ``;``
  (sequence).
- Redirects: ``>`` (truncate), ``>>`` (append), ``<`` (input) — each
  followed by exactly one path token, and each must be the LAST thing
  in its segment (no argv token after a redirect's path — see
  :class:`ExecPlanRejected`'s own docstring for why this is a
  deliberate v1 narrowing, not an oversight).

## What this parser rejects

- A literal newline anywhere in the input (:func:`_reject_raw_newline`).
- Any of :data:`_ALWAYS_FORBIDDEN_CHARS`, quoted or not
  (:func:`_reject_always_forbidden_characters`).
- An UNQUOTED occurrence of one of :data:`_PUNCTUATION_CHARS` that is
  not a supported operator/redirect shape — command substitution
  (backtick), subshell grouping (``(...)``), process substitution
  (``<(...)``/``>(...)``), heredoc (``<<``), a bare ``&`` (background,
  not part of ``&&``), or an unrecognised punctuation run (``;;``,
  ``<>``).
- A leading ``NAME=value`` environment-assignment prefix — every
  character in ``FOO=bar`` is individually allowed, but in the LEADING
  position of a segment it would become that segment's ``argv[0]``,
  hiding the REAL command from tool-axis policy entirely (``FOO=bar
  rm -rf /tmp/x`` → this parser's ``argv[0]`` would be ``"FOO=bar"``,
  never ``"rm"`` — the same basename-bypass class Codex #28732 named,
  reached through assignment instead of a relative path). A LATER
  argument shaped like ``NAME=value`` (``docker run -e FOO=bar image``)
  is unaffected.
- A QUOTED token whose dequoted value happens to consist ENTIRELY of
  :data:`_PUNCTUATION_CHARS` characters (e.g. ``grep '|' file``) —
  string-identical to a real unquoted operator once ``shlex`` strips
  the quotes, so this parser cannot verify which the shell would really
  do; see :func:`_iter_tokens`'s own docstring for how this is detected
  and why it is refused rather than guessed either way.
- An empty command line, an empty segment (a leading/trailing/doubled
  chain operator), a redirect with no following path, an argument after
  a redirect's target, or an unterminated quote (``shlex`` itself
  raises for the last one).

Rejection is LOUD (a raised exception with a human-readable reason),
never a silent pass-through — matching Claude Code's "解析不能なら
プロンプトへ" / Codex's ``used_complex_parsing`` escalation / OpenClaw's
"chain/redirect は全 segment が通らない限り拒否" (architect's own
competitive summary): every competitor's shape is "refuse, don't guess."

## If your command gets rejected

This parser accepts a narrower set of commands than a real shell does
— a real, benign command using one of :data:`_ALWAYS_FORBIDDEN_CHARS`
(a literal ``$``, a glob, brace expansion, home-directory ``~``, a
literal ``!``) is refused rather than guessed at, quoted or not — for
example ``curl "http://x/a?b=1"`` cannot be accepted here: ``?`` is not
distinguishable by quoting, the same reason a genuinely benign use is
indistinguishable from a glob. Two ways forward, once #5838's later
stages land: (1) the no-shell argv form (#5837 — quote nothing, no
operators, exactly the literal words to run) stays available wherever
this parser's caller also offers it; (2) if a specific character
genuinely needs support, it can be added to :data:`_PUNCTUATION_CHARS`
(if quote-aware handling is possible for it) with the same measured
justification every existing entry carries — file it rather than
working around this parser.
"""
from __future__ import annotations

import io
import re
import shlex
from dataclasses import dataclass
from typing import cast

# #5838 BLOCKING (architect's SECOND ruling, issuecomment-5578535394 --
# a self-correction of the first allowlist, issuecomment-5578494687):
# the raw-string-wide allowlist over-rejected. Real-machine measurement
# against 16 real commands found 2 false rejections and a boundary that
# was never "which characters are dangerous" -- it was "which
# characters shlex's own `punctuation_chars` distinguishes by quoting":
#
#   'python -c "print(1)"' -> ['python','-c','print(1)']       (quoted: shlex keeps the parens in the WORD)
#   'python -c print(1)'   -> ['python','-c','print','(','1',')']  (unquoted: shlex splits them into their OWN tokens)
#
# For `( ) | & ; < >` (and backtick, added to punctuation_chars the
# same way), `shlex` genuinely tells quoted from unquoted apart --
# quoted, they land inside a WORD token exactly like sh would treat
# them (literal); unquoted, they become their OWN punctuation token,
# checked below for a SUPPORTED shape (a pipe, a chain op, a redirect)
# and rejected otherwise (an unsupported one, e.g. a bare `(` subshell
# open, still raises). `curl "...?b=1"` and `curl ...?b=1` tokenize
# IDENTICALLY either way (`?` is not in punctuation_chars) -- for THAT
# class of character, quoting changes nothing shlex can see, so this
# parser genuinely cannot recover intent and rejects unconditionally,
# quoted or not (see `_ALWAYS_FORBIDDEN_CHARS` below).
_PUNCTUATION_CHARS = "();<>|&`"

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

# #5838 BLOCKING (architect's second ruling, same comment as above): a
# character shlex does NOT distinguish by quoting -- `$` (variable
# expansion), `*`/`?`/`[`/`]` (glob), `{`/`}` (brace expansion, macOS
# `/bin/sh` expands `{a,b}` even in POSIX mode), `~` (home-directory
# expansion), `!` (negation operator -- `! ls` reads as negation to a
# real shell, never a command name). None of these are in
# `_PUNCTUATION_CHARS`, so `shlex` tokenizes a quoted and an unquoted
# occurrence THE SAME WAY (unlike `( ) | & ; < >`, above) -- there is no
# recoverable signal to tell "the caller quoted this deliberately" from
# "this is about to expand," so every occurrence is rejected, quoted or
# not (`curl "...?b=1"` cannot be saved this way -- architect's own
# explicit acceptance of that loss, see the module docstring's own
# "If your command gets rejected" section for the escape hatch).
_ALWAYS_FORBIDDEN_CHARS = frozenset("$*?[]{}~!")


def _reject_always_forbidden_characters(token: str) -> None:
    """Raises :class:`ExecPlanRejected` if *token* contains any
    character from :data:`_ALWAYS_FORBIDDEN_CHARS` — see that constant's
    own module-level comment for why these, specifically, are rejected
    regardless of quoting (unlike the operator characters in
    :data:`_PUNCTUATION_CHARS`, which ARE quote-aware)."""
    hit = _ALWAYS_FORBIDDEN_CHARS.intersection(token)
    if hit:
        raise ExecPlanRejected(
            f"character(s) not supported here (token: {token!r}, "
            f"character(s): {sorted(hit)!r}) — this parser cannot predict "
            "what these resolve to at execution time, quoted or not (see "
            "the module's own docstring, \"If your command gets "
            "rejected,\" for what to do next)"
        )


def _reject_raw_newline(text: str) -> None:
    """Raises :class:`ExecPlanRejected` if *text* contains a literal
    newline — the ONE check this parser still runs against the RAW
    string, before any tokenizing (module docstring's own newline
    section): ``shlex`` folds a newline into ordinary whitespace, so it
    never reaches a per-token check like
    :func:`_reject_always_forbidden_characters` at all."""
    if "\n" in text or "\r" in text:
        raise ExecPlanRejected(
            "a newline is not supported here — shlex folds it into "
            "ordinary whitespace, but a real shell treats it as a "
            "command separator; this parser cannot tell a newline "
            "inside quotes from one that would start a second, "
            "unreviewed command, and refuses to guess"
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
    character it cannot resolve safely (:data:`_ALWAYS_FORBIDDEN_CHARS`,
    a raw newline, or an unquoted :data:`_PUNCTUATION_CHARS` occurrence
    in an unsupported shape), or a shell construct this parser cannot
    safely decompose into policy-checkable segments (#5838 段2) — see
    this module's own docstring, "What this parser rejects" and
    "Denylist -> two-tier allowlist," for the reasoning.

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
    it resembles) and refuses to guess. NOT the same situation
    :func:`_reject_always_forbidden_characters` handles — those
    characters are rejected because ``shlex`` gives NO quoting signal at
    all; this one exists because ``shlex`` gives a signal
    (:data:`_PUNCTUATION_CHARS`) that this specific case cannot fully
    trust either.

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

    The FIRST check (module docstring's own "Denylist -> two-tier
    allowlist" section) is :func:`_reject_raw_newline`, run against
    *text* BEFORE any tokenizing: ``shlex`` folds a newline into
    ordinary whitespace, so a per-token check would never see it —
    ``"ls\\nrm -rf /tmp/x"`` would otherwise parse as ONE segment whose
    ``argv[0]`` is the ordinary, almost-certainly-allowed ``"ls"``,
    while a real shell runs the newline as a command SEPARATOR, running
    ``rm -rf /tmp/x`` as a second command policy never saw at all.
    Every other rejection (:func:`_reject_always_forbidden_characters`,
    the leading-assignment check, an unsupported operator shape) runs
    per TOKEN, once tokenizing has happened — see the module docstring
    for why these two checks are not merged into one."""
    _reject_raw_newline(text)
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
            _reject_always_forbidden_characters(token)
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
            _reject_always_forbidden_characters(nxt[0])
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

        # An unquoted _PUNCTUATION_CHARS token that survived _iter_tokens
        # but is not one of #5838's supported operators/redirects --
        # "(" / ")" / "`" (subshell / command substitution -- see the
        # module docstring for why these hide a second command), a lone
        # "&" (background, not part of "&&"), or an unrecognised run
        # like "<>"/";;". A QUOTED occurrence of any of these never
        # reaches here at all -- it stays inside a WORD token
        # (_iter_tokens's own classification), accepted as literal text.
        raise ExecPlanRejected(
            f"unsupported shell construct (token: {token!r}) — this parser "
            "only supports pipes/chains (| && || ;) and redirects (> >> <), "
            "see the module docstring for the full rejection list"
        )

    _flush_segment(require_nonempty=not redirect_closed_segment, context="at end of input")
    if not plan or not any(isinstance(item, ExecSegment) for item in plan):
        raise ExecPlanRejected("no command given")
    return plan
