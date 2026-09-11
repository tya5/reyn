"""#6061 — a hand-maintained table of exec "wrapper" binaries: binaries whose
own ``argv[0]`` the tool axis checks, but which then run a DIFFERENT binary
named in their OWN arguments (``env ls`` is checked as ``env``; what runs is
``ls``). See ``exec_plan_policy.py``'s own docstring for how this table is
consumed.

## Why this table is CURATED, never derived (read before adding an entry)

#6110 ruled "母集団は corpus から導出, never a hand-typed list" for
``exec_plan.py``'s own node-kind allowlist — that ruling does NOT apply
here, and conflating the two is the mistake this module's own issue thread
(#6061) caught lead-coder making once already. The difference:

- A grammar node KIND is a property of the GRAMMAR — parsing a corpus of
  real shell one-liners enumerates it, because the population lives in the
  grammar itself (``bash_node_kinds.py``).
- Whether a binary named ``env`` treats its own arguments as "the next
  program to run" is a property of THAT BINARY'S OWN SEMANTICS — world
  knowledge no corpus in this repository contains. Parsing ``env`` a
  thousand times never tells you ``env`` execs its argument; only reading
  ``env``'s own manual does.

There is no corpus to derive this table FROM. Architect's own competitive
research (#6061 issue thread) found that Claude Code, OpenAI Codex, and
Google Gemini CLI all ship the identical shape: a STATIC, hand-written table
of wrapper names, none of them derived from any corpus, all shipped anyway
— and each company's OWN table names a DIFFERENT set of wrappers (Codex
checks ``env``; Claude Code does not). "There is no industry-standard
table" was itself the finding — only industry-standard CHOICES exist, and
each competitor documents what its own choice does not cover, in the same
place the table lives. This module does the same: every entry below is
required to carry both its own justification (why this binary belongs
here) and, where its extraction rule is incomplete, an explicit note of the
forms it does NOT handle — never a silent gap.

## Scope of the initial table — deliberately narrow

Six names, matching the exact 6 the #6061 issue thread measured against
`origin/main` as of this table's creation (12 concrete command lines,
collapsing to 6 wrapper binaries): ``env``, ``xargs``, ``sh`` (the ``-c``
form only), ``timeout``, ``nice``, ``command``. ``sudo`` and ``find -exec``
are DELIBERATELY not entered — their own option grammar was measured as
"not yet measured for extraction difficulty" (#6061, architect) rather than
silently dropped; see :data:`UNSUPPORTED_WRAPPERS` below, which names them
and says why they are absent, rather than leaving a reader to wonder
whether they were considered at all.

## What "cannot extract" means, and why it is a SEPARATE outcome from
"not a wrapper"

:func:`extract_inner_argv` returns ``None`` in two structurally different
cases a caller must NOT conflate:

1. *name* is not a registered wrapper at all (see :func:`is_registered_
   wrapper`) — an ordinary, non-wrapping binary. The correct caller
   response is to STOP unwrapping; there is nothing more to look at.
2. *name* IS a registered wrapper, but this specific invocation's flag/arg
   shape is one this table's extraction rule does not cover (e.g. ``env
   -u NAME cmd`` — ``-u`` takes a separate value argument this table does
   not attempt to parse). The correct caller response, when a tool-axis
   restriction is actually active, is to DENY — the alternative (silently
   treating an unrecognised form as "nothing to unwrap") would let exactly
   the class of command this table exists to catch through unexamined.

:func:`is_registered_wrapper` is what tells these two apart; callers must
check it BEFORE reading a ``None`` from :func:`extract_inner_argv` as
"stop, nothing more to check" — see ``exec_plan_policy.py``'s own
``_check_segment`` for the consuming shape.

## What this table does NOT attempt

- Full option-grammar parsing for any wrapper. Each entry's own docstring
  says exactly which forms it extracts and which it refuses (returns
  ``None`` for) rather than guessing.
- Recursively re-parsing an ``sh -c``'s own command STRING through
  ``exec_plan.py``'s grammar. Per architect's own ruling (#6061 issue
  thread), the extraction for ``sh -c`` takes only the string's OWN first
  whitespace-separated token as the next name to check — a conservative
  approximation, not a second parse of an embedded shell command line.
- Completeness. No wrapper table can be complete (this module's own
  docstring, first section) — see :data:`UNSUPPORTED_WRAPPERS`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# A bare `KEY=VALUE` assignment token, e.g. `env`'s own argument-position
# environment overrides (`env FOO=1 ls`). Matches POSIX environment-variable
# name rules (leading letter/underscore, then alnum/underscore).
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A plain, unsigned integer duration with an optional single-letter unit
# suffix (GNU `timeout DURATION`, e.g. `5`, `5s`, `2m`) — the ONLY duration
# shape #6061's own measurement exercised (`timeout 5 ls`).
_DURATION_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[smhd]?$")

# An old-style `nice`/`renice` adjustment (`-N`, e.g. `-5`) — distinct from
# a `-`-prefixed FLAG because it is digits-only after the sign.
_NICE_OLD_STYLE_ADJUSTMENT_RE = re.compile(r"^-[0-9]+$")


@dataclass(frozen=True)
class WrapperSpec:
    """One curated table entry. *name* is the resolved basename this entry
    matches (``exec_plan_policy.py`` resolves ``argv[0]`` through the SAME
    version-manager-shim resolution the rest of that module already uses,
    then reduces to a basename, before ever consulting this table).

    *rationale* is the 1-line "why does this binary belong in this table"
    this module's own docstring requires of every entry — never omitted,
    even for an obvious case ("this is genuinely obvious" is itself a
    1-line rationale).

    *unsupported_forms* names, in prose, the argv shapes THIS entry's own
    :attr:`extract` will refuse (return ``None`` for) — required even when
    short ("only the bare form"), so a reader of the table sees the limit
    without having to read the extraction function's own source."""

    name: str
    rationale: str
    unsupported_forms: str
    extract: "Callable[[tuple[str, ...]], tuple[str, ...] | None]"


def _extract_env(argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """``env [-i] [KEY=VALUE ...] COMMAND [ARG ...]``. Skips leading
    ``KEY=VALUE`` assignment tokens and the bare, no-value ``-i``/
    ``--ignore-environment`` flag; refuses (returns ``None``) at the first
    OTHER ``-``-prefixed token — several real ``env`` flags (``-u NAME``,
    ``-C DIR``, ``-S STRING``, ``--split-string=STRING``) take a SEPARATE
    value argument this table does not attempt to parse, and guessing
    wrong here would silently under-count the inner argv."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if _ASSIGNMENT_RE.match(tok):
            i += 1
            continue
        if tok in ("-i", "--ignore-environment"):
            i += 1
            continue
        if tok.startswith("-"):
            return None  # an env flag this table does not parse (may take a value)
        break
    return tuple(argv[i:])


def _extract_xargs(argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """``xargs COMMAND [ARG ...]`` — ONLY the bare form (no flags at all),
    matching Claude Code's own documented rule (competitive research,
    #6061 issue thread, quoted verbatim there): "Stripping applies only
    when xargs has no flags: an invocation like xargs -n1 grep pattern is
    matched as an xargs command." Any ``-``-prefixed token anywhere in
    ``argv[1:]`` refuses extraction — xargs' own flags change what runs
    (``-I{}`` substitution, ``-0``/``-d`` delimiter, ``-P`` parallelism),
    and a bare heuristic cannot safely re-derive that."""
    if any(tok.startswith("-") for tok in argv[1:]):
        return None
    return tuple(argv[1:])


def _extract_sh_c(argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """``sh -c 'COMMAND STRING' [ARG0 ...]`` — extracts only the FIRST
    whitespace-separated token of the command STRING, never a full
    re-parse of it (architect's own ruling, #6061 issue thread: "sh は
    -c の次の1語"). Refuses unless argv is EXACTLY ``("sh", "-c",
    <string>, ...)`` — any other flag before ``-c`` (``sh -e -c ...``) or
    a missing ``-c`` (``sh script.sh``) is out of this entry's scope."""
    if len(argv) < 3 or argv[1] != "-c":
        return None
    command_string = argv[2]
    parts = command_string.split(None, 1)
    if not parts:
        return None
    return (parts[0],)


def _extract_timeout(argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """``timeout [OPTION] DURATION COMMAND [ARG ...]``. Skips a single
    leading plain-duration token (``5``, ``5s``, ``2m`` — the only shape
    #6061's own measurement exercised, ``timeout 5 ls``). Refuses at any
    ``-``-prefixed token (``-s SIGNAL``, ``-k DURATION``, ``--foreground``
    — several take a separate value this table does not parse) and at any
    other non-duration, non-flag first token (malformed input to
    ``timeout`` itself — not this table's job to validate)."""
    if len(argv) < 2:
        return None
    first = argv[1]
    if first.startswith("-"):
        return None
    if not _DURATION_RE.match(first):
        return None
    return tuple(argv[2:])


def _extract_nice(argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """``nice [-n ADJUSTMENT | -ADJUSTMENT] COMMAND [ARG ...]``. Handles
    the bare form (``nice ls``), ``-n ADJUSTMENT`` (``nice -n 5 ls``,
    #6061's own measured form), and the old-style ``-ADJUSTMENT`` (``nice
    -5 ls``). Refuses at any other ``-``-prefixed token."""
    if len(argv) < 2:
        return None
    if not argv[1].startswith("-"):
        return tuple(argv[1:])
    if argv[1] == "-n":
        if len(argv) < 4:
            return None
        return tuple(argv[3:])
    if _NICE_OLD_STYLE_ADJUSTMENT_RE.match(argv[1]):
        return tuple(argv[2:])
    return None


def _extract_command(argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """``command COMMAND [ARG ...]`` — ONLY the bare form (#6061's own
    measured form, ``command ls``). Refuses at any ``-``-prefixed token
    (``-p``/``-v``/``-V`` are real ``command`` flags this table does not
    parse)."""
    if len(argv) < 2 or argv[1].startswith("-"):
        return None
    return tuple(argv[1:])


_WRAPPERS: "dict[str, WrapperSpec]" = {
    "env": WrapperSpec(
        name="env",
        rationale=(
            "env's whole documented purpose is to run its OWN argument as a "
            "program, optionally under a modified environment -- #6061's own "
            "lead-coder ruling picks this as the wrapper furthest from "
            '"the binary that actually runs" (env FOO=1 CMD reads as `env` '
            "to the tool axis while CMD is what executes)."
        ),
        unsupported_forms=(
            "Any env flag that takes a separate value argument (-u NAME, "
            "-C DIR, -S STRING, --split-string=STRING, ...) -- only -i / "
            "--ignore-environment (bare, no value) and leading KEY=VALUE "
            "assignments are handled."
        ),
        extract=_extract_env,
    ),
    "xargs": WrapperSpec(
        name="xargs",
        rationale=(
            "xargs runs its own trailing argument as a command, appending "
            "stdin-derived arguments -- Claude Code's own shipped behaviour "
            "strips bare xargs for the identical reason (competitive "
            "research, #6061 issue thread)."
        ),
        unsupported_forms=(
            "Any invocation carrying ANY flag (-n, -I, -0, -P, ...) -- "
            "matches Claude Code's own documented limit verbatim: bare "
            "xargs only."
        ),
        extract=_extract_xargs,
    ),
    "sh": WrapperSpec(
        name="sh",
        rationale=(
            "sh -c STRING execs STRING as a shell command line -- the "
            "single most direct wrapper form measured (#6061 issue "
            "thread's own `sh -c ls` example)."
        ),
        unsupported_forms=(
            "Anything but exactly `sh -c STRING [ARG0 ...]` -- a script "
            "path (`sh script.sh`), a flag before -c (`sh -e -c ...`), or "
            "a missing -c. Extraction takes only STRING's first "
            "whitespace-separated token, never a full re-parse of the "
            "embedded command line."
        ),
        extract=_extract_sh_c,
    ),
    "timeout": WrapperSpec(
        name="timeout",
        rationale=(
            "timeout DURATION COMMAND execs COMMAND once DURATION is "
            "consumed -- #6061's own measurement (`timeout 5 rm -rf "
            "/tmp/x`) is the example a naive argv[0]-only check misreads "
            "as `timeout`."
        ),
        unsupported_forms=(
            "Any timeout OPTION before the duration (-s SIGNAL, -k "
            "DURATION, --foreground, ...) -- several take a separate "
            "value argument this table does not parse. Only a single "
            "leading plain-duration token is handled."
        ),
        extract=_extract_timeout,
    ),
    "nice": WrapperSpec(
        name="nice",
        rationale=(
            "nice [ADJUSTMENT] COMMAND execs COMMAND at an adjusted "
            "scheduling priority -- #6061's own measured forms (`nice "
            "ls`, `nice -n 5 ls`)."
        ),
        unsupported_forms=(
            "Any -flag other than `-n ADJUSTMENT` or the old-style "
            "`-ADJUSTMENT` digits-only form."
        ),
        extract=_extract_nice,
    ),
    "command": WrapperSpec(
        name="command",
        rationale=(
            "the `command` builtin execs its own argument directly, "
            "bypassing shell function/alias lookup -- #6061's own "
            "measured form (`command ls`)."
        ),
        unsupported_forms="Any -flag (-p / -v / -V) -- bare form only.",
        extract=_extract_command,
    ),
}

#: Named wrappers this table does NOT cover, with why — per #6061's own
#: ruling: "取り出せない形が在るなら、それを entry 自身に書く" applied at
#: the table's own edge, not just inside a covered entry. Written here so
#: a reader of this module sees they were CONSIDERED, not forgotten.
UNSUPPORTED_WRAPPERS: "dict[str, str]" = {
    "sudo": (
        "sudo execs its own trailing argument (optionally after option "
        "flags: -u USER, -g GROUP, -E, ...); #6061's own architect ruling "
        "left this table's initial version to the 6 forms actually "
        "measured and named sudo's own option-parsing difficulty as "
        "explicitly UNMEASURED, not merely deferred by oversight."
    ),
    "find": (
        "find ... -exec COMMAND {} \\; (or the + terminator) execs "
        "COMMAND once per matched path; the argv shape needs a scan for "
        "the -exec token AND its own \\;/+ terminator, unmeasured for "
        "extraction difficulty (#6061 issue thread, architect)."
    ),
}


def is_registered_wrapper(name: str) -> bool:
    """True iff *name* (a resolved basename, e.g. ``"env"``) has a table
    entry — the FIRST check a caller must make; see this module's own
    docstring, "What 'cannot extract' means"."""
    return name in _WRAPPERS


def extract_inner_argv(name: str, argv: "tuple[str, ...]") -> "tuple[str, ...] | None":
    """The inner argv *name*'s own table entry extracts from *argv* (whose
    ``argv[0]`` is *name*'s own invocation token), or ``None`` if this
    entry's extraction rule does not cover *argv*'s specific shape (see
    this module's own docstring — callers must have already confirmed
    :func:`is_registered_wrapper` before treating a ``None`` return as
    "cannot extract" rather than "not a wrapper").

    Raises :class:`KeyError` if *name* has no table entry at all — callers
    must check :func:`is_registered_wrapper` first; this is not a
    graceful-degradation path, it is a programming-error guard (the two
    ``None`` meanings this module's docstring distinguishes must never be
    collapsed by a caller skipping the registration check)."""
    return _WRAPPERS[name].extract(argv)
