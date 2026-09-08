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

## What this parser accepts

- ``shlex``-quoted words — single/double quotes, backslash escapes;
  the SAME primitive #5837's own (now-superseded) ``tokenize_exec_
  cmdline`` used for the no-shell argv form.
- Chain operators: ``|`` (pipe), ``&&`` (AND), ``||`` (OR), ``;``
  (sequence).
- Redirects: ``>`` (truncate), ``>>`` (append), ``<`` (input) — each
  followed by exactly one path token, and each must be the LAST thing
  in its segment (no argv token after a redirect's path — see
  :class:`ExecPlanRejected`'s own docstring for why this is a
  deliberate v1 narrowing, not an oversight).

## What this parser REJECTS (raises :class:`ExecPlanRejected`)

Anything that would let a segment's real argv[0] diverge from what this
parser saw — the parse/execute gap class above:

- Command substitution: ``$(...)`` or backtick ```cmd```` — a nested
  command this parser would never see, exactly the "解析器が見ない
  第2の経路" shape.
- Subshell grouping: ``(...)``  / process substitution: ``<(...)``,
  ``>(...)`` — same reason; a parenthesized group can hide a whole
  second pipeline.
- Heredoc: ``<<`` (and ``<<-``) — arbitrary multi-line content the
  segment-level policy (段3) was never designed to scan.
- Background execution: a bare ``&`` (not part of ``&&``) — changes
  process lifecycle in a way #5838's plan never modeled.
- Variable/glob/home-directory expansion: any token containing ``$``,
  a glob metacharacter (``* ? [``), or starting with ``~`` — resolves
  to something DIFFERENT at real execution time than the literal text
  this parser saw (measured bypasses: a redirect target check would
  see the literal string ``"$HOME/out.txt"``, never the real expanded
  path; a threat scan over ``rm *.txt`` would see the literal glob,
  never the files it actually matches).
- A leading ``NAME=value`` environment-assignment prefix — would become
  the segment's ``argv[0]``, hiding the REAL command from tool-axis
  policy entirely (``FOO=bar rm -rf /tmp/x`` → this parser's ``argv[0]``
  would be ``"FOO=bar"``, never ``"rm"`` — the same basename-bypass
  class Codex #28732 named, reached through assignment instead of a
  relative path).
- A QUOTED token whose dequoted value happens to consist entirely of
  operator characters (e.g. ``grep '|' file``) — string-identical to a
  real unquoted operator once ``shlex`` strips the quotes, so this
  parser cannot verify which the shell would really do; see
  :func:`_iter_tokens`'s own docstring for how this is detected and why
  it is refused rather than guessed either way.
- Any other unsupported punctuation sequence (``;;``, ``<>``, an
  isolated stray operator character, etc.).
- An empty command line, an empty segment (a leading/trailing/doubled
  chain operator), a redirect with no following path, or an unterminated
  quote (``shlex`` itself raises for the last one).

Rejection is LOUD (a raised exception with a human-readable reason),
never a silent pass-through — matching Claude Code's "解析不能なら
プロンプトへ" / Codex's ``used_complex_parsing`` escalation / OpenClaw's
"chain/redirect は全 segment が通らない限り拒否" (architect's own
competitive summary): every competitor's shape is "refuse, don't guess."
"""
from __future__ import annotations

import io
import re
import shlex
from dataclasses import dataclass
from typing import cast

# The operator/redirect characters this parser understands as
# standalone tokens — a token whose dequoted VALUE is entirely made of
# these characters is either a real operator (unquoted) or, if it was
# quoted, rejected outright (_iter_tokens's own docstring — this parser
# cannot tell the two apart by value alone, and refuses to guess).
# Backtick is added to shlex's own default punctuation set
# (`shlex.punctuation_chars = True` gives ``();<>|&`` per the stdlib
# docs) specifically so command substitution's backtick form is caught
# by the SAME "not a recognised operator" rule as ``$(...)``'s own
# ``(``/``)`` — one rejection path, not two.
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

# #5838 BLOCKING (architect co-vet, issuecomment-5578395294, real-machine
# measurement against this module): a token containing ``$`` (variable
# expansion), a glob metacharacter (``* ? [``), or starting with ``~``
# (home-directory expansion) resolves to something DIFFERENT at real
# `sh -c` execution time than the literal text this parser sees --
# exactly the same "parser saw one thing, shell runs another" class this
# module's own docstring names for `$(...)`/backtick, just via expansion
# instead of substitution. Measured real bypasses: `echo hi >
# $HOME/out.txt` let a redirect-target permission check (段3's future
# `require_file_write`) see the literal string `"$HOME/out.txt"` instead
# of the real path; `rm *.txt` let a threat scan see `"*.txt"` instead of
# whatever files actually match at execution time. The SAME
# "no way to know what this really is" gap a quoted operator-shaped
# token (_iter_tokens's own docstring) is rejected for -- this closes
# the inconsistency architect's own co-vet named: that gap was closed
# for quoting and left open for expansion.
_EXPANSION_CHARS = frozenset("$*?[")

# #5838 BLOCKING (same co-vet): `FOO=bar rm -rf /tmp/x` -- a leading
# `NAME=value` environment-assignment prefix -- is not stripped by this
# parser, so `argv[0]` becomes the literal string `"FOO=bar"`, never
# `"rm"`. Any future tool-axis policy (段3) reading `argv[0]` would
# check the WRONG binary entirely; the real `rm` never reaches policy at
# all -- the exact `./sed` basename-bypass class architect's own
# competitive research (Codex #28732) names, reached through a different
# door. Matched only in LEADING position (before the first real word of
# a segment — see `_looks_like_leading_assignment`'s own caller) so a
# legitimate later argument shaped like `NAME=value` (e.g. `docker run
# -e FOO=bar image`) is unaffected: `docker` already satisfied "a real
# word was seen" before `FOO=bar` appears.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _contains_expansion(token: str) -> bool:
    """True if *token* would resolve to something this parser cannot
    predict at real shell execution time — see ``_EXPANSION_CHARS``'s
    own module-level comment for the measured bypasses this closes."""
    return bool(_EXPANSION_CHARS.intersection(token)) or token.startswith("~")


def _looks_like_leading_assignment(token: str) -> bool:
    """True if *token* has the ``NAME=value`` shape of a shell
    environment-assignment prefix — see ``_ASSIGNMENT_RE``'s own
    module-level comment for the measured bypass this closes. Callers
    apply this ONLY while still in a segment's leading position."""
    return bool(_ASSIGNMENT_RE.match(token))


class ExecPlanRejected(Exception):
    """Raised by :func:`parse_exec_plan` when *text* contains a shell
    construct this parser cannot safely decompose into policy-checkable
    segments (#5838 段2) — see this module's own docstring for the full
    rejection list and why each one is there.

    This is a v1 narrowing, disclosed, not a claim of covering every
    legitimate shell command: a real, benign command using a rejected
    construct (a heredoc, a subshell, `2>` fd-numbered redirects, argv
    tokens after a redirect) is refused with a reason, the same
    "refuse, don't guess" shape every competitor #5838's own research
    found already takes."""


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
    :data:`_EXPANSION_CHARS` applies to ``$``/glob/``~``.

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
    for why)."""
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
            if _contains_expansion(token):
                raise ExecPlanRejected(
                    f"variable/glob/home-directory expansion is not supported "
                    f"here (token: {token!r}) — this parser cannot predict "
                    "what it resolves to at execution time, the same "
                    "uncertainty a quoted operator is rejected for"
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
            if _contains_expansion(nxt[0]):
                raise ExecPlanRejected(
                    f"variable/glob/home-directory expansion is not "
                    f"supported in a redirect target (token: {nxt[0]!r}) — "
                    "a future file-permission check would see this literal "
                    "text, never the real expanded path"
                )
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
        # of #5838's supported operators/redirects -- "(", ")", "`", a
        # lone "&", or an unrecognised run like "<>"/";;".
        raise ExecPlanRejected(
            f"unsupported shell construct (token: {token!r}) — this parser "
            "only supports pipes/chains (| && || ;) and redirects (> >> <), "
            "see the module docstring for the full rejection list"
        )

    _flush_segment(require_nonempty=not redirect_closed_segment, context="at end of input")
    if not plan or not any(isinstance(item, ExecSegment) for item in plan):
        raise ExecPlanRejected("no command given")
    return plan
