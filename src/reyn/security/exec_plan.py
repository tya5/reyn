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

## Disclosed v1 limitation — a QUOTED operator-only token

``shlex`` strips quotes before this module ever sees the resulting
token value, so ``grep '|' file`` (a literal pipe character as an
argument, quoted) is currently indistinguishable from an unquoted ``|``
by VALUE alone once tokenizing is done — this parser rejects it as an
operator. This is the SAFE direction (a false rejection, never a false
acceptance that could misread a real operator as literal text) —
"refuse, don't guess" holds even where the guess would have been
right. Fixing it needs per-token quoting metadata ``shlex``'s public
iterator does not expose; tracked as a known follow-up, not solved
here.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

# The operator/redirect characters this parser understands as
# standalone tokens — everything else that shlex would otherwise fold
# into a plain word stays a word (e.g. a quoted "|" is protected by
# shlex's own quote handling, never reaches here as a punctuation
# token at all). Backtick is added to shlex's own default punctuation
# set (`shlex.punctuation_chars = True` gives ``();<>|&`` per the
# stdlib docs) specifically so command substitution's backtick form is
# caught by the SAME "not a recognised operator" rule as ``$(...)``'s
# own ``(``/``)`` — one rejection path, not two.
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
    own, only classifies what ``shlex`` already produced. A quoted
    ``'|'`` is never one of these: ``shlex``'s own quote handling keeps
    it as part of a plain WORD token, so quoting something is exactly
    what protects it from being read as an operator here — the same
    guarantee real POSIX shell quoting gives.

    Raises :class:`ExecPlanRejected` for an unterminated quote (``shlex``
    itself raises ``ValueError``)."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCTUATION_CHARS)
    lexer.whitespace_split = True
    try:
        raw_tokens = list(lexer)
    except ValueError as exc:
        raise ExecPlanRejected(f"could not parse arguments: {exc}") from exc

    operator_chars = frozenset(_PUNCTUATION_CHARS)
    return [
        (token, all(c in operator_chars for c in token))
        for token in raw_tokens
    ]


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
