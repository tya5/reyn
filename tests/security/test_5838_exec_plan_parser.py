"""Tier 1: #5838 段2 — ``ExecPlan`` parser (owner decision: parse a shell
command line into policy-checkable segments, execute the ORIGINAL string
via ``sh -c`` in a later stage). Covers:

- Accept: plain argv, quoted words, every supported chain operator
  (``| && || ;``), every supported redirect (``> >> <``), and
  architect's own worked example (multi-segment pipe + redirect).
- Reject: empty input, unterminated quotes, command substitution
  (``$(...)``/backtick), subshell grouping (``(...)``), heredoc
  (``<<``), a bare ``&``, an empty segment (leading/trailing/doubled
  operator), a redirect with no target, an argv token after a
  redirect's target.
- The plan's own shape matches architect's example exactly (real data,
  not a paraphrase of the docstring).

No mocking — ``parse_exec_plan`` is a pure function over a string;
every test drives it directly.
"""
from __future__ import annotations

import pytest

from reyn.security.exec_plan import (
    ExecChainOp,
    ExecPlanRejected,
    ExecRedirect,
    ExecSegment,
    parse_exec_plan,
)

# ── accept ────────────────────────────────────────────────────────────


def test_plain_argv_is_one_segment() -> None:
    """Tier 1: no operators at all -> a single ExecSegment, argv order
    preserved exactly."""
    assert parse_exec_plan("ls -la /tmp") == [ExecSegment(argv=("ls", "-la", "/tmp"))]


def test_quoted_word_with_space_stays_one_argv_token() -> None:
    """Tier 1: shlex quoting behaves identically to #5837's own
    (now-superseded) tokenize_exec_cmdline for this case."""
    assert parse_exec_plan("ls 'my dir'") == [ExecSegment(argv=("ls", "my dir"))]


@pytest.mark.parametrize("op", ["|", "&&", "||", ";"])
def test_each_chain_operator_splits_into_two_segments(op: str) -> None:
    """Tier 1: every one of #5838's 4 supported chain operators produces
    exactly [segment, ExecChainOp(op), segment] — the SAME shape for
    all 4, not special-cased per operator."""
    plan = parse_exec_plan(f"a {op} b")
    assert plan == [
        ExecSegment(argv=("a",)),
        ExecChainOp(op=op),
        ExecSegment(argv=("b",)),
    ]


@pytest.mark.parametrize("op", ["|", "&&", "||", ";"])
def test_each_chain_operator_works_unspaced(op: str) -> None:
    """Tier 1: real shells don't require whitespace around an operator
    (``a|b`` pipes exactly like ``a | b``) — this parser must match
    that, not just the spaced form."""
    plan = parse_exec_plan(f"a{op}b")
    assert plan == [
        ExecSegment(argv=("a",)),
        ExecChainOp(op=op),
        ExecSegment(argv=("b",)),
    ]


@pytest.mark.parametrize("op", [">", ">>", "<"])
def test_each_redirect_attaches_after_its_segment(op: str) -> None:
    """Tier 1: every one of #5838's 3 supported redirects produces
    [segment, ExecRedirect(op, path)] — the redirect is its own plan
    item, not folded into the segment's own argv."""
    plan = parse_exec_plan(f"cat {op} out.txt")
    assert plan == [
        ExecSegment(argv=("cat",)),
        ExecRedirect(op=op, path="out.txt"),
    ]


def test_architects_own_worked_example_matches_exactly() -> None:
    """Tier 1: the literal example from #5838's own implementation plan
    (architect, issuecomment-5557091059) — pinned as the acceptance
    shape, not re-derived from this parser's own internals."""
    plan = parse_exec_plan("ls -la | grep foo > out.txt")
    assert plan == [
        ExecSegment(argv=("ls", "-la")),
        ExecChainOp(op="|"),
        ExecSegment(argv=("grep", "foo")),
        ExecRedirect(op=">", path="out.txt"),
    ]


def test_multiple_chained_segments() -> None:
    """Tier 1: three segments joined by ``;`` — not just the 2-segment
    minimal case."""
    plan = parse_exec_plan("a ; b ; c")
    assert plan == [
        ExecSegment(argv=("a",)),
        ExecChainOp(op=";"),
        ExecSegment(argv=("b",)),
        ExecChainOp(op=";"),
        ExecSegment(argv=("c",)),
    ]


# ── reject: empty / malformed input ──────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_empty_or_whitespace_only_is_rejected(text: str) -> None:
    """Tier 1: no command at all — never a silent no-op plan."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_unterminated_quote_is_rejected() -> None:
    """Tier 1: shlex's own ValueError is folded into ExecPlanRejected,
    not left to propagate as a different exception type callers would
    need a second catch clause for."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("ls 'unterminated")


# ── reject: constructs that could hide a segment from policy ─────────


@pytest.mark.parametrize("text", [
    "echo $(date)",
    "echo `date`",
    "echo $(rm -rf /)",
])
def test_command_substitution_is_rejected(text: str) -> None:
    """Tier 1: #5838's own core rejection reason — a nested command this
    parser would never see is exactly the parse/execute-gap class
    architect's own competitive research (Codex #28732 basename bypass)
    names.

    Strip witness: removing ``(``/``)``/backtick from
    ``_PUNCTUATION_CHARS`` makes ``$(...)``'s contents parse as an
    ordinary argv word instead of raising — verified directly during
    authoring, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", ["(ls)", "( ls; pwd )"])
def test_subshell_grouping_is_rejected(text: str) -> None:
    """Tier 1: a parenthesized group can hide a whole second pipeline
    this parser would never see — same class as command substitution."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_heredoc_is_rejected_with_a_specific_reason() -> None:
    """Tier 1: heredoc gets its OWN rejection message (distinguishable
    from the generic "unsupported construct" reason) — its body is
    arbitrary multi-line content, a different hazard shape from a
    single-token construct like ``$(...)``."""
    with pytest.raises(ExecPlanRejected, match="heredoc"):
        parse_exec_plan("cat << EOF")


def test_bare_background_ampersand_is_rejected() -> None:
    """Tier 1: only ``&&`` is a supported operator — a lone ``&``
    (background execution) is not in #5838's plan."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("ls &")


# ── reject: malformed operator/redirect placement ─────────────────────


@pytest.mark.parametrize("text", ["| ls", "ls |", "a || | b", "a | | b"])
def test_an_empty_segment_around_a_chain_operator_is_rejected(text: str) -> None:
    """Tier 1: a leading, trailing, or doubled chain operator leaves an
    empty command on one side — never silently dropped/treated as a
    no-op segment. Includes ``"a | | b"`` (spaced double pipe, a real
    POSIX shell syntax error) alongside ``"a || | b"``."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_redirect_with_no_target_is_rejected() -> None:
    """Tier 1: a redirect operator with nothing after it — never a
    silent no-op redirect."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("ls >")


def test_redirect_with_no_preceding_command_is_rejected() -> None:
    """Tier 1: a redirect with no command before it — there is nothing
    for the redirect to attach to."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("> out.txt")


def test_argument_after_a_redirect_target_is_rejected() -> None:
    """Tier 1: a deliberate v1 narrowing (module docstring) — redirects
    must be the last thing in a segment here, even though real shells
    allow ``cmd arg1 > file arg2`` (equivalent to ``cmd arg1 arg2 >
    file``). Rejecting rather than silently mis-attributing ``arg2``."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("ls > out.txt -la")


def test_chained_redirects_on_the_same_segment_are_accepted() -> None:
    """Tier 1: ``cmd > out.txt < in.txt`` — two redirects, no argv
    between them, on the same command — must not be confused with the
    "argument after a redirect" rejection above."""
    plan = parse_exec_plan("cat > out.txt < in.txt")
    assert plan == [
        ExecSegment(argv=("cat",)),
        ExecRedirect(op=">", path="out.txt"),
        ExecRedirect(op="<", path="in.txt"),
    ]


# ── reject: real-machine measured bypasses (lead-coder/architect co-vet,
#    issuecomment-5578395294 / issuecomment-5578405655) ──────────────────


@pytest.mark.parametrize("text", [
    "FOO=bar rm -rf /tmp/x",
    "PATH=/tmp/evil ls",
])
def test_leading_environment_assignment_is_rejected(text: str) -> None:
    """Tier 1: architect's own real-machine measurement against this
    module before this fix — a leading ``NAME=value`` prefix became
    this parser's ``argv[0]``, hiding the real command (``rm``/``ls``)
    from any future tool-axis policy entirely, the same basename-bypass
    class Codex #28732 named.

    Strip witness: removing the leading-assignment check in
    ``parse_exec_plan``'s word-token branch makes ``argv[0]`` equal the
    literal ``"FOO=bar"``/``"PATH=/tmp/evil"`` string instead of raising
    — verified directly, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_environment_assignment_as_a_later_argument_is_accepted() -> None:
    """Tier 1: the leading-only scope of the check above — a
    ``NAME=value``-shaped token appearing AFTER a real command word
    (``docker`` here) is a legitimate argument (``docker run -e
    FOO=bar image``), not an environment-assignment prefix, and must
    stay accepted."""
    plan = parse_exec_plan("docker run -e FOO=bar image")
    assert plan == [
        ExecSegment(argv=("docker", "run", "-e", "FOO=bar", "image")),
    ]


@pytest.mark.parametrize("text", ["ls | FOO=bar rm", "ls && FOO=bar rm"])
def test_leading_assignment_is_rejected_in_every_segment_not_just_the_first(text: str) -> None:
    """Tier 1: architect's own explicit re-check (issuecomment-5578436446)
    — the leading-assignment guard must re-apply at the START OF EVERY
    segment, not just the plan's first one; a chain operator resets
    "leading position" for the segment that follows it."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", [
    "echo hi > $HOME/out.txt",
    "echo hi > ~/out.txt",
    "rm *.txt",
    "echo $FOO",
    "echo a?b",
    "echo [ab]",
])
def test_variable_glob_or_home_expansion_is_rejected(text: str) -> None:
    """Tier 1: architect's own real-machine measurement — a token
    containing ``$``/glob metacharacters/leading ``~`` resolves to
    something DIFFERENT at real ``sh -c`` execution time than the
    literal text this parser sees (measured: a redirect-target
    permission check would see the literal string ``"$HOME/out.txt"``,
    never the real expanded path; a threat scan over ``rm *.txt`` would
    see the literal glob, never the files it actually matches). ``$``
    closes structurally now (#5987 stage 2 — a ``simple_expansion``/
    ``expansion``/``concatenation`` node outside
    :data:`_ALLOWED_NODE_KINDS`); glob/``~`` close via the disclosed
    leaf-text exception, :data:`_EXPANSION_LEAF_CHARS` — quoting doesn't
    change what either sees, unlike :data:`_PUNCTUATION_CHARS`'s own
    quote-aware group.

    Strip witness: removing ``simple_expansion``/``expansion`` from the
    disallowed-kinds check and dropping ``*``/``?``/``[``/``]``/``~``
    from :data:`_EXPANSION_LEAF_CHARS` makes each case parse
    successfully with the literal expansion syntax as a plain argv/path
    token instead of raising — verified directly, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", [
    "cp file{1,2} /tmp/",
    "echo {a,b}.txt",
])
def test_brace_expansion_is_rejected(text: str) -> None:
    """Tier 1: architect's own follow-up real-machine measurement
    (issuecomment-5578436446) — macOS ``/bin/sh`` expands ``{a,b}``
    even in POSIX mode, so ``cp file{1,2} /tmp/`` parses to ONE argv
    token (``"file{1,2}"``) while the real shell operates on TWO files
    — the same "cannot predict what this resolves to" class as ``$``/
    glob/``~``. Closes structurally now (#5987 stage 2) — ``{``/``}``
    adjacent to other word text produce a ``concatenation`` node, which
    is outside :data:`_ALLOWED_NODE_KINDS`.

    Strip witness: allowlisting ``concatenation`` makes both of these
    parse successfully with the literal brace syntax instead of raising
    — verified directly, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


# ── the two-tier check itself (architect's SECOND ruling, correcting ────
#    the first raw-string-wide allowlist — issuecomment-5578535394) ─────


@pytest.mark.parametrize("text", ["! ls", "ls; ! rm -rf /tmp/x", "ls && ! rm -rf /tmp/x"])
def test_negation_operator_is_rejected(text: str) -> None:
    """Tier 1: architect's own real-machine measurement while writing
    the allowlist ruling — a real shell reads a leading ``!`` as the
    NEGATION operator (``! false; echo $?`` -> ``0``), never a command
    name, but this parser (pre-fix) read ``argv[0]='!'`` and let the
    real command after it (``rm -rf /tmp/x``) ride along unseen by any
    future policy. Closes structurally now (#5987 stage 2) — a leading
    ``!`` parses to a ``negated_command`` node, outside
    :data:`_ALLOWED_NODE_KINDS`.

    Strip witness: allowlisting ``negated_command`` makes each of these
    parse successfully with ``'!'`` as a literal argv token instead of
    raising — verified directly, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", [
    "echo x!y",
    "echo x\\$y",
    "echo {",
    "echo x\\{y",
    "echo x\\}y",
])
def test_leaf_only_expansion_chars_are_still_rejected(text: str) -> None:
    """Tier 1: closes a corpus gap a #6098 BLOCKING review round found —
    a real-execution co-vet compared this PR against ``origin/main`` and
    concluded ``$``/``{``/``}``/``!`` in :data:`_EXPANSION_LEAF_CHARS`
    were unreachable dead weight (every existing case in this file's own
    69-input corpus closes earlier, via a distinct grammar node kind:
    ``simple_expansion``/``expansion``/``concatenation``/
    ``negated_command``). That conclusion held for every input this
    corpus HAD, not for the character in general: a non-leading ``!``
    (no ``negated_command``), a backslash-escaped ``$``/``{``/``}``, and
    a brace character standing alone (no adjacent word text to form a
    ``concatenation`` with) all parse to a plain ``word``/
    ``string_content`` leaf — verified directly against the installed
    ``tree-sitter-bash`` grammar. Narrowing :data:`_EXPANSION_LEAF_CHARS`
    to just the glob/tilde characters (as the same review round first
    proposed) would have silently ACCEPTED all 5 of these.

    Only the REJECTION is asserted, not which mechanism catches it — the
    whole point of #5987 stage 2 is that the node-kind path and the
    leaf-text path can each independently close a given input, and which
    one fires for a given shape is an implementation detail this test
    must not pin (that would make it an algorithm-level pin on internal
    routing, forbidden by this repo's testing policy)."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_an_always_forbidden_character_inside_quotes_is_still_rejected() -> None:
    """Tier 1: this parser's leaf-text scan (:data:`_EXPANSION_LEAF_CHARS`,
    #5987 stage 2) is checked per AST leaf node, quote-position-blind, on
    purpose — ``tree-sitter-bash`` parses ``'$HOME'`` as a literal
    ``raw_string`` (single quotes suppress expansion, correctly, unlike
    double quotes), but this parser still scans that leaf's own text and
    rejects the ``$`` it finds there, so there is no signal this parser
    could use to trust the quoting even if it wanted to (module
    docstring's own "#5987 stage 2" section, the `curl "...?b=1"`
    example)."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("echo '$HOME'")


def test_a_punctuation_char_becomes_literal_only_when_quoted() -> None:
    """Tier 1: architect's OWN corrected worked example (issuecomment-
    5578535394) — the exact case the first (raw-string) allowlist got
    wrong. ``python -c "print(1)"`` must be ACCEPTED (``shlex`` keeps
    the parens inside the quoted WORD token, matching how ``sh`` itself
    would read them: literal); ``python -c print(1)`` — the SAME
    characters, unquoted — must be REJECTED (``shlex`` splits them into
    their own punctuation tokens, an unsupported "(" construct). Only
    testing ONE direction would not be a witness for this: the design's
    entire claim is that quoting changes the outcome for
    :data:`_PUNCTUATION_CHARS` characters specifically."""
    assert parse_exec_plan('python -c "print(1)"') == [
        ExecSegment(argv=("python", "-c", "print(1)")),
    ]
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("python -c print(1)")


def test_a_non_punctuation_forbidden_char_stays_rejected_even_quoted() -> None:
    """Tier 1: the sibling contrast to the test above — for
    :data:`_EXPANSION_LEAF_CHARS` (unlike :data:`_PUNCTUATION_CHARS`),
    quoting changes NOTHING this parser's leaf-text scan can see, so
    ``curl "http://x/a?b=1"`` and ``curl http://x/a?b=1`` must BOTH
    reject — architect's own explicit acceptance of this loss (module
    docstring's own "If your command gets rejected" section)."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan('curl "http://x/a?b=1"')
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("curl http://x/a?b=1")


@pytest.mark.parametrize("char", sorted("$*?[]{}~!"))
def test_every_expansion_shaped_character_is_individually_rejected(char: str) -> None:
    """Tier 1: #5987 stage 2 — was a direct population test of the now-
    removed ``_ALWAYS_FORBIDDEN_CHARS`` private constant; rewritten to a
    literal population instead of importing private state, per this
    repo's testing policy. Every one of these characters — the same set
    the old character allowlist enumerated — must still cause a
    rejection on its own when embedded in an otherwise-ordinary word,
    whether the constraint reaches it structurally (the node-kind
    allowlist, e.g. ``$``/``{``/``}``/``[``/``]`` via a ``concatenation``
    node) or via the disclosed leaf-text exception for glob/tilde/``!``
    (module docstring's own "#5987 stage 2" section) — the SAME
    rejection outcome, whichever path catches it. Catches an
    accidentally-narrowed set (a typo dropping a character) as surely as
    one that widened."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(f"echo x{char}y")


def test_a_stray_closing_brace_alone_is_rejected() -> None:
    """Tier 1: a stray ``}`` alone (``echo a}b``) is not a real
    brace-expansion pattern and carries no divergence risk on its own,
    but ``tree-sitter-bash`` still parses ``a}b`` as a ``concatenation``
    node (outside :data:`_ALLOWED_NODE_KINDS`) same as it would for a
    real ``{a,b}`` pair — this parser has no per-construct carve-out
    mechanism for it. architect's own explicit acceptance of this exact
    over-rejection: a safe-side false positive."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("echo a}b")


def test_quoted_operator_shaped_token_is_rejected_not_silently_split() -> None:
    """Tier 1: lead-coder's own additional real-machine measurement —
    ``grep '|' file`` (a literal pipe character as a quoted argument)
    used to be silently accepted and split into TWO piped commands
    (``grep`` | ``file``) instead of the one ``grep`` call the caller
    actually wrote, because a quoted token's dequoted value is
    string-identical to a real unquoted operator once shlex strips the
    quotes. Now rejected outright — this parser cannot verify which the
    real shell would do, and refuses to guess (the same judgement
    ``_EXPANSION_CHARS`` above applies).

    Strip witness: reverting ``_iter_tokens`` to classify purely by
    token VALUE (dropping the raw-substring quote check) makes this
    silently return a 2-segment plan (``grep`` piped to ``file``)
    instead of raising — verified directly, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("grep '|' file")


# ── reject: a literal newline (worst of the class, architect co-vet) ────


@pytest.mark.parametrize("text", ["ls\nrm -rf /tmp/x", "echo a\necho b", "ls\r\nrm -rf /"])
def test_a_literal_newline_is_rejected(text: str) -> None:
    """Tier 1: architect's own real-machine measurement
    (issuecomment-5578466297) — the worst bypass this module closes.
    ``shlex`` folds a newline into ordinary whitespace, so
    ``"ls\\nrm -rf /tmp/x"`` parsed as ONE segment whose ``argv[0]`` is
    the ordinary, almost-certainly-allowed ``"ls"`` — while a real
    shell runs the newline as a command SEPARATOR, executing ``rm -rf
    /tmp/x`` as a SECOND command policy never saw at all. Unlike every
    other bypass this module closes, ``argv[0]`` here is completely
    unremarkable, so a future tool-axis policy (段3) would PASS this
    plan outright.

    Strip witness: removing the newline pre-check at the top of
    ``parse_exec_plan`` makes this parse successfully into ONE segment
    (``('ls', 'rm', '-rf', '/tmp/x')``) instead of raising — verified
    directly, restored after."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_redirect_fd_duplication_stays_rejected() -> None:
    """Tier 1: architect's own confirmed-safe case — ``2>&1`` (fd
    duplication, not in this parser's supported redirect set) was
    already rejected before the newline fix and must stay that way."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan("2>&1")


def test_shell_style_comment_stays_accepted() -> None:
    """Tier 1: architect's own confirmed-safe case — ``shlex``'s
    default comment handling (``#`` to end of line) already matches
    real shell behavior, so ``ls # rm -rf /`` correctly parses to just
    ``('ls',)`` — the comment is not silently smuggling a second
    command past this parser, it genuinely is inert on both sides."""
    assert parse_exec_plan("ls # rm -rf /") == [ExecSegment(argv=("ls",))]
