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
2. (HISTORICAL — see "#5987 stage 2" below for the CURRENT mechanism)
   a module-level ``_ALWAYS_FORBIDDEN_CHARS`` set (`$ * ? [ ] { } ~ !`)
   used to be checked per ``shlex`` token, quote-blind, because
   ``shlex`` tokenizes these THE SAME WAY whether quoted or not, so
   there was no recoverable signal. #5987 stage 2 replaced that
   per-token check with a node-kind allowlist walked over a real
   ``tree-sitter-bash`` parse; only the residual, structurally
   undetectable slice (glob/tilde — :data:`_EXPANSION_LEAF_CHARS`)
   still works this way, now scanning AST leaf nodes rather than
   ``shlex`` tokens.

(HISTORICAL) A raw-newline check used to run against the RAW string
before any tokenizing, because ``shlex`` folds a newline into ordinary
whitespace and a per-token check would never see it. #5987 stage 2
replaced this with a structural check over the AST (see below) — the
grammar's own parse tree shows a raw newline as two adjacent statement
nodes with nothing joining them, a signal ``shlex`` itself cannot
produce.

## #5987 stage 2 — the classification UNIT is now a real grammar's node
kind, not a character count (owner directive "調べて" — architect's
research; lead-coder's staging ruling, issuecomment-5618328297)

This section used to say a CHARACTER allowlist is not what competitors
do, and that ``shlex`` has no grammar to allowlist nodes OF, so
counting characters was the only mechanism available. **That is no
longer true.** #5987 stage 1 (:mod:`reyn.security.bash_node_kinds`)
built :func:`~reyn.security.bash_node_kinds.derive_bash_node_kind_names`
— the real, current set of ``tree-sitter-bash`` grammar node-kind names,
machine-derived from the INSTALLED package at call time, never a
hand-typed list. Stage 2 (this change) wires that population into
classification: :func:`parse_exec_plan` now parses *text* with
``tree-sitter-bash`` first (:func:`_reject_via_node_kind_classification`)
and walks the resulting syntax tree, rejecting outright if the tree has
a parse error, if the top-level statements are adjacent with no
supported separator between them (the AST's own structural signature
for a raw newline — ``shlex`` folds a newline into whitespace and would
never see it; the real grammar's parser simply produces two sibling
statement nodes with nothing joining them, distinguishable from an
explicit ``;`` because that token itself still appears as a sibling),
or if ANY named node in the tree is not one of :data:`_ALLOWED_NODE_KINDS`
— the same unit Claude Code and Codex both use (architect's competitive
research). Every one of :data:`_ALLOWED_NODE_KINDS` is verified (by
this module's own test suite) to be a member of
:func:`~reyn.security.bash_node_kinds.derive_bash_node_kind_names`'s
real, current output — never a hand-typed string that might not exist
in the installed grammar.

This closes a REAL gap the character approach could not: bash's own
grammar has node kinds for command substitution (`` `...` ``/``$(...)``),
subshell grouping (``(...)``), process substitution (``<(...)``/``>(...)``),
heredocs, leading environment-assignment prefixes
(``variable_assignment``), and — critically — shell CONTROL STRUCTURES
(``if``/``for``/``while``/``case``/function definitions). The old
character-only approach had no way to see an ``if``/``for`` statement at
all (none of its characters were individually forbidden) and would
silently have accepted one as an ordinary chain of argv segments; the
node-kind allowlist rejects every one of these by construction (they
are not in :data:`_ALLOWED_NODE_KINDS`), fail-closed on an UNKNOWN kind
exactly as it does on a KNOWN-dangerous one (gap D, unchanged — see
:class:`ExecPlanRejected`'s own docstring: unknown constructs are
REJECTED, never escalated to an operator prompt, in this stage).

**What did NOT move to the node-kind unit, and why.** ``bash``'s own
grammar — mirrored faithfully by ``tree-sitter-bash`` — does not give
pathname glob expansion (``*``/``?``/``[...]``) or tilde expansion
(``~``) their own syntax node at all when they appear as an ordinary
word character: both are resolved by the shell's EXECUTION engine after
parsing, not by its parser, so ``rm *.txt`` and ``rm foo.txt`` produce
structurally IDENTICAL trees (both: a single ``word`` node). No grammar
node kind exists for this module to allowlist or reject — the
"structural signal" this design otherwise relies on genuinely is not
there. :func:`_reject_via_node_kind_classification` therefore also scans
the TEXT of the tree's own leaf nodes (``word``/``string_content``/
``raw_string`` — i.e. only nodes the grammar has already validated as
one of :data:`_ALLOWED_NODE_KINDS`) for :data:`_EXPANSION_LEAF_CHARS`
(``$*?[]{}~!``) and rejects a hit the same way regardless of quoting —
the same conservative, quote-blind judgement the old
:data:`_ALWAYS_FORBIDDEN_CHARS` check made, relocated onto the AST's own
leaf nodes instead of raw ``shlex`` tokens. This is not a character
allowlist doing the SAME job as before under a new name: for every
construct the grammar CAN see (``$HOME`` expansion, ``{a,b}`` brace
expansion via a ``concatenation`` node, ``!`` negation via
``negated_command``, a raw newline via the adjacency check above), the
node-kind allowlist is what actually fires — the leaf-text scan is a
backstop, not the primary mechanism for most of these 9 characters, and
which one fires depends on the SHAPE the character appears in, not the
character alone (measured directly against the installed
``tree-sitter-bash`` grammar, #6098 BLOCKING review round — see the
per-character breakdown below; do not assume any one character is
"handled" by only one of the two mechanisms):

  - ``*`` ``?`` ``[`` ``]`` ``~`` (pathname glob, tilde expansion) —
    ``bash``'s own grammar has NO node of its own for these; every
    shape (adjacent to other word text or standing alone) parses to a
    plain ``word``, so the leaf-text scan is the ONLY mechanism that
    ever catches them.
  - ``$`` ``{`` ``}`` ``!`` — when adjacent to other word text (``$HOME``,
    ``{a,b}``, a leading ``!``), these parse to their OWN node
    (``simple_expansion``/``expansion``, ``concatenation``,
    ``negated_command``) and are caught by the node-kind allowlist
    BEFORE the leaf-text scan ever runs. But a NON-leading ``!``
    (``echo x!y``), a backslash-escaped ``$``/``{``/``}`` (``echo
    x\\$y``), or a brace character with no adjacent word text to form a
    ``concatenation`` with (``echo {`` alone) all parse to a plain
    ``word``/``string_content`` leaf instead — for THESE shapes, the
    leaf-text scan is what actually rejects, and it is the only thing
    that does (a first draft of this doc, and a #6098 review round
    that measured only the corpus already in this module's own test
    file, both read these 4 characters as dead weight the node-kind
    check had already made redundant — false: that conclusion held
    for every input the existing corpus HAD, not for the characters in
    general; see ``test_leaf_only_expansion_chars_are_still_rejected``
    for the corpus gap that closed it and why narrowing this set would
    have silently re-accepted all 5 of its cases).

:data:`_PUNCTUATION_CHARS`'s own quote-aware operator/redirect-shape
classification in :func:`_iter_tokens` is UNCHANGED by this stage — its
job is now building the :data:`ExecPlan`'s segments/chain-ops/redirects
from text the node-kind gate has ALREADY approved, not deciding
acceptability; its own supported-shape checks (a bare ``&``, an
unrecognised punctuation run, a quoted token that is string-identical to
an operator) still run and still reject, now as a redundant backstop —
every shape they reject is independently rejected by the node-kind gate
too (subshell/command-substitution/heredoc/process-substitution nodes,
or the explicit bare-``&`` check in the top-level adjacency walk),
except the quoted-operator-shape check (``grep '|' file``), which stays
the ONE mechanism for that specific case — a real, safe tree (the AST
correctly parses it as one command with a literal ``|`` argument) that
this parser still refuses out of the same "cannot verify, will not
guess" judgement as before; #5987 does not ask this stage to touch it.

This module still makes NO claim of completeness — see
:class:`ExecPlanRejected`'s own docstring and this stage's PR body for
the explicit "does not mean 0 bypasses" disclosure. Switching the
classification unit does not remove the fundamental limitation both
``tree-sitter``-based competitors and this module share: an expansion
target that cannot be resolved statically (this module refuses it; some
competitors escalate it to an operator instead, gap D, still unruled
here) is a real, on-going risk surface a different parser does not make
disappear (Codex's own tree-sitter-based classifier has carried real
bugs — openai/codex#8394, architect's own research on #5987).

## What this parser accepts

- Ordinary word characters — letters, digits, and punctuation neither
  :data:`_PUNCTUATION_CHARS` nor :data:`_EXPANSION_LEAF_CHARS` claims,
  and not part of a node kind outside :data:`_ALLOWED_NODE_KINDS`
  (e.g. ``- _ . / , : = + @ % #``) — literal to both ``tree-sitter-bash``
  and ``sh`` in every position, quoted or not.
- ``shlex``-quoted words — single/double quotes, backslash escapes;
  the SAME primitive #5837's own (now-superseded) ``tokenize_exec_
  cmdline`` used for the no-shell argv form. Quoting DOES let a word
  contain one of :data:`_PUNCTUATION_CHARS`'s own characters as literal
  text (``"print(1)"``) — it does NOT rescue a character from
  :data:`_EXPANSION_LEAF_CHARS` (see the section above for why those
  two groups are treated differently).
- Chain operators: ``|`` (pipe), ``&&`` (AND), ``||`` (OR), ``;``
  (sequence).
- Redirects: ``>`` (truncate), ``>>`` (append), ``<`` (input) — each
  followed by exactly one path token, and each must be the LAST thing
  in its segment (no argv token after a redirect's path — see
  :class:`ExecPlanRejected`'s own docstring for why this is a
  deliberate v1 narrowing, not an oversight).

## What this parser rejects

- A literal newline anywhere in the input — detected structurally now
  (the AST's own adjacent-statements-with-no-separator shape), not by
  scanning *text* for ``\\n`` (:func:`_reject_via_node_kind_classification`).
- Any node kind :func:`parse_exec_plan` does not recognise
  (:data:`_ALLOWED_NODE_KINDS`) — command substitution, subshell
  grouping, process substitution, heredoc, a leading environment
  assignment, negation, brace expansion, and any shell control structure
  (``if``/``for``/``while``/``case``/a function definition) all fall out
  of this one check now, structurally, rather than needing their own
  character rule.
- Any of :data:`_EXPANSION_LEAF_CHARS` (``$ * ? [ ] { } ~ !``) inside a
  leaf ``word``/``string_content``/``raw_string`` node, quoted or not —
  glob/tilde (``* ? [ ] ~``) have no grammar node of their own for ANY
  shape, so the leaf-text scan is their only mechanism; ``$``/``{``/
  ``}``/``!`` are USUALLY caught earlier by the node-kind allowlist
  (their own node when adjacent to other word text) but fall through to
  this same leaf-text scan for a non-leading ``!``, a backslash-escaped
  ``$``/``{``/``}``, or a standalone brace (see the module docstring's
  own "#5987 stage 2" section for the full per-character breakdown and
  why treating these 4 as dead would have been wrong).
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
— a real, benign command using a node kind outside
:data:`_ALLOWED_NODE_KINDS` or one of :data:`_EXPANSION_LEAF_CHARS`
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

import tree_sitter
import tree_sitter_bash

from reyn.security.bash_node_kinds import derive_bash_node_kind_names

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
# quoted or not (HISTORICAL -- see `_EXPANSION_LEAF_CHARS` and
# `_ALLOWED_NODE_KINDS` below, #5987 stage 2, for the CURRENT mechanism).
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

# #5987 stage 2 (lead-coder's ruling, issuecomment-5618328297): the
# classification UNIT for "which shell constructs are safe to accept" --
# module docstring's own "#5987 stage 2" section has the full reasoning.
# Every kind below is a real, currently-existing tree-sitter-bash grammar
# node-kind name -- verified against
# :func:`~reyn.security.bash_node_kinds.derive_bash_node_kind_names`'s
# actual output both here (module import time, fail closed if a future
# tree-sitter-bash bump ever drops/renames one) and by this module's own
# test suite (never a hand-typed string trusted on faith).
_ALLOWED_NODE_KINDS = frozenset({
    "program",
    "command",
    "command_name",
    "word",
    "string",
    "string_content",
    "raw_string",
    "pipeline",
    "list",
    "redirected_statement",
    "file_redirect",
    "comment",
})

# Top-level (``program``-child) node kinds this parser treats as "one
# real statement" for the newline-adjacency check below -- every one of
# these is itself already gated by :data:`_ALLOWED_NODE_KINDS` (nothing
# else reaches this set).
_STATEMENT_NODE_KINDS = frozenset({"command", "list", "pipeline", "redirected_statement"})

# The three LEAF node kinds -- allowed by :data:`_ALLOWED_NODE_KINDS` --
# whose own TEXT this module still scans for
# :data:`_EXPANSION_LEAF_CHARS`. See module docstring's own "#5987 stage
# 2" section: pathname glob (`*`/`?`/`[...]`) and tilde (`~`) expansion
# have no grammar node of their own in tree-sitter-bash (they are
# resolved by the shell's execution engine, not its parser), so a
# structurally-identical tree cannot distinguish `rm *.txt` from `rm
# foo.txt` -- this is the one place this module still reads characters,
# not tokens, and it runs over AST leaves the grammar has ALREADY
# validated as one of :data:`_ALLOWED_NODE_KINDS`, never over raw shlex
# tokens.
_LEAF_TEXT_NODE_KINDS = frozenset({"word", "string_content", "raw_string"})

# Deliberately NOT in _ALLOWED_NODE_KINDS -- kept as their own named set
# only so :func:`_reject_via_node_kind_classification` can give heredoc
# its OWN, more specific rejection reason (matching this module's
# pre-#5987 behaviour) rather than the generic "unsupported node kind"
# message every other disallowed kind gets.
_HEREDOC_NODE_KINDS = frozenset({
    "heredoc_start",
    "heredoc_body",
    "heredoc_end",
    "heredoc_redirect",
})

# `$`/`{`/`}`/`!` stay in this set even though EACH is already caught
# structurally when adjacent to other word text (`simple_expansion`/
# `expansion`, `concatenation`, `negated_command` -- none in
# :data:`_ALLOWED_NODE_KINDS`) -- a non-leading `!` (`echo x!y`), a
# backslash-escaped `$`/`{`/`}` (`echo x\$y`), or a standalone brace
# (`echo {`) all parse to a plain `word`/`string_content` leaf instead,
# same reasoning as the glob/tilde characters (measured directly
# against tree-sitter-bash, #6098 BLOCKING review round -- see module
# docstring's own "#5987 stage 2" section for the full breakdown;
# `test_leaf_only_expansion_chars_are_still_rejected` witnesses all 5
# shapes so a future "these 4 look dead" reading stops at a red test,
# not just this comment).
_EXPANSION_LEAF_CHARS = frozenset("$*?[]{}~!")

# Fail-closed at import time, not just in this module's own test suite
# (lead-coder's stage-1 instruction applied here too): if a future
# tree-sitter-bash release ever renames or drops one of
# :data:`_ALLOWED_NODE_KINDS`, this module refuses to import rather than
# silently allowlisting a node kind that no longer exists (which would
# be inert, not unsafe, but a stale allowlist is exactly the kind of
# thing that must be visible, not silent).
_missing_allowed_kinds = _ALLOWED_NODE_KINDS - derive_bash_node_kind_names()
if _missing_allowed_kinds:
    raise RuntimeError(
        "reyn.security.exec_plan._ALLOWED_NODE_KINDS names node kind(s) "
        f"{sorted(_missing_allowed_kinds)!r} that the INSTALLED "
        "tree-sitter-bash grammar does not have -- refusing to import "
        "with a stale allowlist rather than silently checking against "
        "kinds that can never actually appear"
    )
del _missing_allowed_kinds


def _load_bash_parser() -> "tree_sitter.Parser":
    """Builds a fresh :class:`tree_sitter.Parser` for the installed
    ``tree-sitter-bash`` grammar -- the same grammar-loading primitive
    :mod:`reyn.security.bash_node_kinds` uses to derive node-kind names,
    used here to actually PARSE *text* rather than enumerate the
    grammar's symbol table."""
    language = tree_sitter.Language(tree_sitter_bash.language())
    return tree_sitter.Parser(language)


def _walk_nodes(node: "tree_sitter.Node") -> "list[tree_sitter.Node]":
    """Returns every node in the subtree rooted at *node*, itself
    included, in a stable pre-order (parent before children) --
    :func:`_reject_via_node_kind_classification` uses this to check
    every node in the tree against :data:`_ALLOWED_NODE_KINDS`, not just
    the top level."""
    out = [node]
    for child in node.children:
        out.extend(_walk_nodes(child))
    return out


def _reject_via_node_kind_classification(text: str) -> None:
    """Raises :class:`ExecPlanRejected` if *text*, parsed as
    ``tree-sitter-bash``, contains any construct this parser does not
    recognise as safe -- the #5987 stage 2 classification UNIT (module
    docstring's own "#5987 stage 2" section has the full reasoning).
    Runs BEFORE :func:`_iter_tokens` in :func:`parse_exec_plan` — the
    real accept/reject decision now happens here, against the AST, not
    against ``shlex`` tokens.

    Three checks, in order:

    1. A PARSE ERROR anywhere in the tree (``tree.root_node.has_error``)
       — an unterminated quote, a stray/doubled operator, an incomplete
       redirect, and similar malformed shapes all surface as a parse
       error rather than needing their own rule.
    2. Two top-level statements (:data:`_STATEMENT_NODE_KINDS`) directly
       adjacent with no supported separator between them — the AST's own
       structural signature for a raw newline (``shlex`` folds a newline
       into whitespace and would never see it; a real grammar's parser
       produces two sibling statement nodes with nothing joining them,
       distinguishable from an explicit ``;``, which still appears as
       its own sibling token) — or any other unsupported top-level
       token (a bare ``&``, a stray ``;;``, ...).
    3. Any node in the tree — at any depth — whose kind is not in
       :data:`_ALLOWED_NODE_KINDS`, OR any :data:`_LEAF_TEXT_NODE_KINDS`
       leaf whose own text contains one of :data:`_EXPANSION_LEAF_CHARS`
       (the disclosed exception — see :data:`_LEAF_TEXT_NODE_KINDS`'s own
       comment for why glob/tilde/escaped-``$``/``{``/``}``/non-leading-``!``
       still need this).

    :data:`_PUNCTUATION_CHARS`'s own quote-aware operator/redirect-shape
    checks in :func:`_iter_tokens` still run AFTER this, unchanged — see
    module docstring's own "#5987 stage 2" section for why (a redundant
    backstop for most shapes; the sole remaining primary mechanism for
    the quoted-operator-shape case, ``grep '|' file``)."""
    parser = _load_bash_parser()
    tree = parser.parse(text.encode("utf-8", errors="surrogateescape"))
    root = tree.root_node
    if root.has_error:
        # A heredoc gets its own, more specific reason (matching this
        # module's pre-#5987 behaviour) -- tree-sitter-bash still
        # produces a `heredoc_start` node inside the ERROR subtree for
        # an incomplete heredoc (this parser never accepts one complete
        # either, its body is arbitrary multi-line content no
        # segment-level check was designed to scan), so this is
        # detectable even though the tree as a whole has a parse error.
        if any(node.type == "heredoc_start" for node in _walk_nodes(root)):
            raise ExecPlanRejected(
                "heredoc (\"<<\") is not supported here — this parser "
                "only decomposes a command line into policy-checkable "
                "segments, and a heredoc's body is arbitrary multi-line "
                "content no segment-level check was designed to scan."
            )
        raise ExecPlanRejected(
            "could not parse this as a supported shell construct "
            "(tree-sitter-bash reported a parse error) — see the "
            "module's own docstring, \"If your command gets rejected,\" "
            "for what to do next"
        )

    for node in _walk_nodes(root):
        if node.is_named and node.type not in _ALLOWED_NODE_KINDS:
            if node.type in _HEREDOC_NODE_KINDS:
                raise ExecPlanRejected(
                    "heredoc (\"<<\") is not supported here — this "
                    "parser only decomposes a command line into "
                    "policy-checkable segments, and a heredoc's body is "
                    "arbitrary multi-line content no segment-level check "
                    "was designed to scan."
                )
            raise ExecPlanRejected(
                f"shell construct not supported here (grammar node "
                f"kind: {node.type!r}, text: "
                f"{text[node.start_byte:node.end_byte]!r}) — this "
                "parser only recognises a fixed, allowlisted set of "
                "tree-sitter-bash node kinds (see the module's own "
                "docstring, \"If your command gets rejected,\" for what "
                "to do next)"
            )
        if node.type in _LEAF_TEXT_NODE_KINDS:
            leaf_text = text[node.start_byte : node.end_byte]
            hit = _EXPANSION_LEAF_CHARS.intersection(leaf_text)
            if hit:
                raise ExecPlanRejected(
                    f"character(s) not supported here (text: "
                    f"{leaf_text!r}, character(s): {sorted(hit)!r}) — "
                    "this parser cannot predict what these resolve to "
                    "at execution time, quoted or not (see the module's "
                    "own docstring, \"If your command gets rejected,\" "
                    "for what to do next)"
                )

    prev_was_statement = False
    for child in root.children:
        if child.is_named and child.type in _STATEMENT_NODE_KINDS:
            if prev_was_statement:
                raise ExecPlanRejected(
                    "two commands with no supported separator between "
                    "them — this parser cannot tell whether the "
                    "original text had a newline (a real shell treats "
                    "that as a command separator) or some other "
                    "unreviewed join, and refuses to guess"
                )
            prev_was_statement = True
        elif child.type == ";":
            prev_was_statement = False
        elif child.is_named and child.type == "comment":
            pass
        else:
            raise ExecPlanRejected(
                f"unsupported shell construct at the top level (token: "
                f"{child.type!r}) — this parser only supports "
                "pipes/chains (| && || ;) and redirects (> >> <), see "
                "the module docstring for the full rejection list"
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
    """Raised by :func:`parse_exec_plan` when *text* contains a grammar
    node kind outside :data:`_ALLOWED_NODE_KINDS` (including the AST's
    own structural signature for a raw newline), a
    :data:`_EXPANSION_LEAF_CHARS` character this parser cannot resolve
    safely (glob/tilde/escaped ``$``/``{``/``}``/non-leading ``!``), or
    an unquoted
    :data:`_PUNCTUATION_CHARS` occurrence in an unsupported shape, or a
    shell construct this parser cannot safely decompose into
    policy-checkable segments (#5838 段2) — see this module's own
    docstring, "What this parser rejects" and "#5987 stage 2," for the
    reasoning.

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
    :func:`_reject_via_node_kind_classification` handles — this one
    exists because ``shlex`` gives a signal (:data:`_PUNCTUATION_CHARS`)
    that this specific case cannot fully trust; the AST-based gate
    already ran and ACCEPTED this same text (``tree-sitter-bash``
    correctly parses ``grep '|' file`` as one command with a literal
    ``|`` argument, no ambiguity) — this check stays the one remaining
    PRIMARY mechanism for this specific shape, not a backstop (module
    docstring's own "#5987 stage 2" section).

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

    The FIRST check (module docstring's own "#5987 stage 2" section) is
    :func:`_reject_via_node_kind_classification`, run against the WHOLE
    of *text*, parsed as ``tree-sitter-bash``, BEFORE any ``shlex``
    tokenizing: this is where a node kind outside
    :data:`_ALLOWED_NODE_KINDS`, a :data:`_EXPANSION_LEAF_CHARS`
    character, or the AST's own structural signature for a raw newline
    (``"ls\\nrm -rf /tmp/x"`` — two adjacent top-level statement nodes
    with nothing joining them) gets rejected. Everything after that call
    runs on text this gate has ALREADY approved — the leading-assignment
    check and :data:`_PUNCTUATION_CHARS`'s own supported-operator-shape
    checks still run per TOKEN, once ``shlex`` tokenizing has happened,
    as a second, narrower pass building the actual :data:`ExecPlan`
    (module docstring's own "#5987 stage 2" section for why these are
    not merged into one)."""
    _reject_via_node_kind_classification(text)
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
            # _reject_via_node_kind_classification already rejected every
            # _EXPANSION_LEAF_CHARS occurrence in *text* (and every node
            # kind outside _ALLOWED_NODE_KINDS) before this loop ever
            # ran -- token can only reach here already having passed
            # that gate.
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
            # Same note as above -- nxt[0] already passed
            # _reject_via_node_kind_classification's leaf-text scan.
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
