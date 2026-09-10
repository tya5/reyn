"""Tier 1: #6108 — ``ExecPlan`` parser's ``_ALLOWED_NODE_KINDS`` allowlist,
widened from a DERIVED corpus rather than one more hand-picked addition.

## The regression (#6108)

#6098's node-kind allowlist (12 hand-picked ``tree-sitter-bash`` kinds)
rejected ANY command with a standalone integer argument — ``sleep 5``,
``tail -n 100``, ``head -20``, ``git log -5``, ``kill -9 1234``,
``pytest -x -n 4``, ``curl -m 30``, ``grep -A 3``, ``docker ps -n 5``,
``sort -k 2`` (10 of 13 real commands tried). Root cause: a standalone
integer token parses to its own ``number`` node in ``tree-sitter-bash``,
not ``word`` — ``number`` was never in the 12-kind set.

## Why this is a SEPARATE test file, not more cases in
``test_5838_exec_plan_parser.py``

lead-coder's ruling (issue #6108, verbatim): the 12-kind allowlist was
ALSO hand-picked/imagined, not derived — adding ``number`` alone would
repeat exactly that mistake, just for one more case found today. This
file's job is different from ``test_5838_exec_plan_parser.py``'s: it is
the WITNESS that the allowlist's *accept side* was derived from a real
population, not authored from memory — co-located with the reject-side
4-shape witness below so a future accidental widen that re-opens one of
#5987's closed bypasses is caught in the SAME place as an under-widen
regression like this one (lead-coder's own instruction: "allowlist を触る
人は両方向を見ざるを得ません").

## How the corpus was derived (method, not narrated results)

Three real sources in THIS repo (lead-coder's explicit list; deliberately
NOT ``REYN_LLM_TRACE_DUMP`` — that is the owner's own environment, out of
this session's reach):

1. ``.github/workflows/**`` — every ``run:`` step body, split into its
   individual non-empty, non-comment lines (a `run:` block is one script,
   but treating each line as its own parse unit is MORE conservative for
   this purpose: smaller trees, no risk of one giant command line masking
   what a bare line's own node shape looks like).
2. ``docs/**/*.md`` — every fenced ` ```bash `/` ```sh `/` ```shell ` code
   block, each non-empty, non-comment line (a leading ``$ `` shell-prompt
   marker stripped if present).
3. ``scripts/**/*.py`` — every ``subprocess.run``/``Popen``/``call``/
   ``check_call``/``check_output`` call whose arguments build an actual
   shell command LINE (a single string-literal command, or a
   ``["sh"/"bash", "-c", "<cmd>"]``-shaped list, or a literal command
   string embedded as a constant meant to represent a shell invocation).
   This population is genuinely almost empty: this repo's own
   ``scripts/`` never uses ``shell=True`` and passes exactly ONE
   ``["sh", "-c", ...]``-shaped call anywhere, found by exhaustive
   ``ast``-walk over every ``scripts/**/*.py`` file plus a manual grep
   for ``shell=True``/``"-c"``/``'-c'`` to catch a string-embedded
   ``sh -c`` invocation the AST walk's narrow shape wouldn't itself
   match (``scripts/sandbox_seccomp_x86_64_live_smoke.py``'s own
   ``nested_pipe_code`` string constant, whose embedded command —
   ``"ls / | wc -l"`` — is included below).

Extraction produced 1,855 raw lines (419 workflow, 1,436 doc, 1 script) →
1,143 unique command-line strings after dedup. Every unique string was
parsed with the SAME ``tree_sitter.Parser``/``_load_bash_language``
machinery ``exec_plan.py``/``bash_node_kinds.py`` already use (never a
second parser), and every NAMED node kind across every successfully-parsed
tree (286 of the 1,143 hit a genuine ``tree-sitter-bash`` parse error —
expected: markdown prose, YAML `${{ }}` interpolation left un-substituted,
and similar non-command text living inside a fenced/`run:` block; these
are real corpus noise, not evidence about what a real COMMAND needs, so
they contribute nothing to the union) was collected into one union set.

## The derived union, and what actually got added

The union contained 12 + 17 = 29 distinct node kinds. The 12 already in
:data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` were all present
(expected — real commands use ordinary words/pipes/redirects constantly).
Of the 17 NEW kinds the corpus surfaced, only ONE was added:

- **``number``** — the exact #6108 regression. Present in 67 of the 1,143
  unique corpus lines (14 from real `run:` steps, 53 from doc examples),
  including real CI lines that were being wrongly rejected before this
  fix: ``exit 0``, ``exit 1``, ``sleep 720``,
  ``head -40 release-notes.md``, and
  ``pytest tests/scripts/test_check_doc_drift_5003.py -k 5010 -v``. A
  plain, argument-shape leaf node with no interaction with any of
  #5987's closed bypasses (a bare integer carries no expansion, no
  substitution, no control-flow signal) — this is what lead-coder's
  ruling calls "argument-shape nodes like `number`, and similar
  plain-value node kinds."

The other 16 were each present in the real corpus too (several in actual
``run:`` lines, not just doc noise — see the per-kind counts below) but
were deliberately NOT added, flagged here instead per lead-coder's point
5 ("do NOT add it silently"):

- **``command_substitution``**, **``subshell``**,
  **``process_substitution``** — the three constructs #5987 stage 2's own
  module docstring names EXPLICITLY as meant to stay rejected
  ("critically... command substitution... subshell grouping... process
  substitution... heredoc"). Present in the corpus (11/9/4 occurrences
  respectively) because real CI/doc examples legitimately use
  ``$(...)``/``(... )``/``<(...)`` — but accepting these would reopen the
  exact parse/execute semantic gap #5838's whole design exists to close
  (a nested command this parser would never see). **Flagged, not added.**
- **``simple_expansion``**, **``expansion``**, **``special_variable_name``**,
  **``variable_name``**, **``regex``** — the ``$``-expansion family
  (``$VAR``, ``${VAR}``, ``$?``, ``$!``, ``${VAR#pattern}``). This is the
  STRUCTURAL mechanism #5987 stage 2 relies on to reject one of the 4
  closed bypass shapes (a real value substituted at execution time that
  this parser can never see literally — the exact
  ``test_variable_glob_or_home_expansion_is_rejected`` case in
  ``test_5838_exec_plan_parser.py``). Widening any of these would
  silently re-open that bypass. **Flagged, not added.**
- **``concatenation``** — the STRUCTURAL mechanism catching brace
  expansion (``{a,b}``) and non-simple ``$``-adjacency, the second of the
  4 closed bypass shapes (``test_brace_expansion_is_rejected``).
  **Flagged, not added.**
- **``variable_assignment``**, **``declaration_command``**, **``array``**
  — adjacent to the leading-``NAME=value``-assignment bypass class
  (#5838 BLOCKING, ``test_leading_environment_assignment_is_rejected``):
  a standalone ``FOO=bar`` currently fails the node-kind gate itself
  (``variable_assignment`` is not allowed) before the parser's own
  positional regex check even runs — allowing this node kind would
  remove that first line of defense, leaving only the regex-based
  re-check as a backstop instead of two independent mechanisms.
  ``declaration_command`` (``export FOO=bar``) and ``array``
  (``FOO=(...)`` ) share the same paren/assignment-adjacent shape.
  **Flagged, not added — needs a closer, deliberate look if ever
  requested, not a corpus-driven auto-widen.**
- **``herestring_redirect``** (``<<<``) — a redirect SHAPE not in
  :data:`~reyn.security.exec_plan._REDIRECT_OPS_SINGLE`/
  ``_REDIRECT_OPS_DOUBLE`` at all (only ``> >> <`` are supported); a new
  data-flow shape this parser has never reasoned about. **Flagged, not
  added.**
- **``file_descriptor``** (``2>/dev/null``, fd-numbered redirects) — the
  module's own docstring (``ExecPlanRejected``'s own docstring) already
  DISCLOSES fd-numbered redirects as an intentional v1 narrowing, not a
  bug (``test_redirect_fd_duplication_stays_rejected`` pins this).
  Widening this needs a deliberate redirect-handling change, not a
  silent node-kind add. **Flagged, not added.**
- **``test_command``**, **``unary_expression``** — both traced back to a
  SINGLE corpus occurrence, ``[--resume]`` from a doc's CLI usage-syntax
  line (``docs/guide/for-reyn-developers/run-swe-bench.md``) — not a real
  ``[ ... ]`` shell test invocation at all, just square-bracket usage
  notation misread as bash syntax by the extraction. Too thin a signal
  to trust as "a real command needs this." **Flagged, not added.**

## What this file adds structurally

1. The accept-side corpus test below (:func:`test_curated_real_corpus_...`)
   — a CURATED subset of the real, found corpus (the full 1,143-line set
   is too large and too noisy — see the parse-error count above — to
   commit verbatim; this subset is drawn directly from it, not
   authored), asserting every one includes at least the ``number``-fix
   witnesses (commands that were rejected before this PR and are
   accepted after) alongside a broader accept-side sample.
2. The SAME reject-side 4-shape witness
   (:func:`test_5838_exec_plan_parser`'s own shapes: ``$``/glob/``~``
   expansion, brace expansion, negation, a literal newline) co-located
   here — the over-widening witness, so touching
   :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` again means
   reading both directions in the SAME file.

Strip witness (verified directly during authoring, restored after — an
Edit-based break/restore in ``exec_plan.py`` itself, not
``git checkout``/``stash``/``restore``): removing ``"number"`` from
:data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` makes every
``number``-dependent corpus command below (``sleep 720``, ``exit 0``,
``exit 1``, ``head -40 release-notes.md``,
``pytest ... -k 5010 -v``) raise :class:`~reyn.security.exec_plan.ExecPlanRejected`
again — confirming :func:`test_curated_real_corpus_commands_are_accepted`
actually exercises the fix rather than passing vacuously.
"""
from __future__ import annotations

import pytest

from reyn.security.exec_plan import ExecPlanRejected, parse_exec_plan

# ── accept: the derived, curated real-command corpus ────────────────────
#
# Every entry below is a REAL command line found verbatim in this repo (see
# module docstring's own "How the corpus was derived" section) -- none are
# authored-for-the-test strings. Each tuple is (source, command_text); the
# source is retained so a future re-derivation can trace an entry back to
# where it came from.
_REAL_CORPUS: "list[tuple[str, str]]" = [
    # -- the #6108 regression itself: real commands rejected before this
    #    PR, accepted after (a standalone `number` node) --
    ("pytest tests/scripts/test_check_doc_drift_5003.py -k 5010 -v", ".github/workflows/check-doc-drift-fixture.yml"),
    ("exit 0", ".github/workflows/check-doc-drift.yml"),
    ("exit 1", ".github/workflows/main-sweep-mirror.yml"),
    ("head -40 release-notes.md", ".github/workflows/release.yml"),
    ("sleep 720", ".github/workflows/test.yml"),
    ("sleep 3", "docs/deep-dives/contributing/dogfood-discipline.md"),
    (
        "reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5 --baseline smoke-v1",
        "docs/concepts/observability/dogfood-scenarios.ja.md",
    ),
    (
        'python scripts/hn_research.py --topic "AI agent" --max-results 10 --top-comments 5',
        "docs/deep-dives/contributing/dogfood-discipline.md",
    ),
    (
        "python scripts/cleanup_agent_worktrees.py --force --keep-recent 5",
        "docs/reference/agent-worktree-cleanup.ja.md",
    ),
    ("reyn config set safety.loop.max_router_iterations 50", "docs/reference/cli/config.md"),
    ("reyn mcp serve --project /path/to/your/project --timeout 180", "docs/reference/cli/mcp.ja.md"),
    ("reyn web --port 9000 --log-level debug", "docs/reference/cli/web.md"),
    ("python scripts/llm_replay.py abc123 --trace .reyn/llm_trace.jsonl --n 10", "docs/reference/dogfood-tracing.md"),
    ("reyn web --host 0.0.0.0 --port 8080", "docs/guide/for-users/chat-and-web-ui.ja.md"),
    # -- already-accepted-before-#6108 real commands, kept for broad
    #    accept-side coverage (not every corpus command needs `number`) --
    (
        "python -m pytest tests/security/test_landlock_exec_shim_1344e.py -v -rs --timeout=120",
        ".github/workflows/sandbox-landlock-deny-gate.yml",
    ),
    ("pip install -e . -c ci-constraints.txt", ".github/workflows/audit-event-firing-condition-gate.yml"),
    ("python scripts/bash_node_kind_count_ratchet.py", ".github/workflows/bash-node-kind-count-ratchet.yml"),
    ("cat .reyn-ci-stall-trace.log", ".github/workflows/test.yml"),
    ("mkdocs build --strict -f .mkdocs/mkdocs.yml", ".github/workflows/pages.yml"),
    ("python -m build", ".github/workflows/release.yml"),
    ("pip install --upgrade build twine", ".github/workflows/release.yml"),
    ("free -m", ".github/workflows/test.yml"),
    ("ps -ef", ".github/workflows/test.yml"),
    ("pgrep -a python", ".github/workflows/test.yml"),
    # -- the one scripts/**-sourced command (see module docstring's own
    #    "How the corpus was derived", source 3) --
    ("ls / | wc -l", "scripts/sandbox_seccomp_x86_64_live_smoke.py (embedded sh -c string constant)"),
]


@pytest.mark.parametrize("text,source", _REAL_CORPUS, ids=[s for _, s in _REAL_CORPUS])
def test_curated_real_corpus_commands_are_accepted(text: str, source: str) -> None:
    """Tier 1: every command here is REAL — found verbatim in this repo's
    own ``.github/workflows/**``, ``docs/**``, or ``scripts/**`` (module
    docstring has the full derivation method) — not authored for this
    test. ``parse_exec_plan`` must accept every one of them; a future
    narrowing of :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS`
    that breaks any single entry here is exactly the #6108 regression
    class this file exists to catch."""
    parse_exec_plan(text)  # must not raise


# ── reject: the 4 closed bypass shapes stay rejected (over-widening ─────
#    witness -- co-located with the accept-side corpus above per
#    lead-coder's own instruction) ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["echo $FOO", "echo hi > $HOME/out.txt", "rm *.txt", "echo hi > ~/out.txt"],
)
def test_dollar_glob_or_tilde_expansion_stays_rejected(text: str) -> None:
    """Tier 1: the first of #5987's 4 closed bypass shapes. Must stay
    rejected no matter how :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS`
    is widened in the future — a real shell resolves ``$``/glob/``~`` at
    EXECUTION time, something this parser can never see literally
    (`test_5838_exec_plan_parser.py`'s own
    ``test_variable_glob_or_home_expansion_is_rejected`` is the primary
    witness; this is the over-widening witness co-located with #6108's
    accept-side corpus)."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", ["cp file{1,2} /tmp/", "echo {a,b}.txt"])
def test_brace_expansion_stays_rejected(text: str) -> None:
    """Tier 1: the second closed bypass shape — real shells expand
    ``{a,b}`` (even POSIX-mode ``/bin/sh`` on macOS), producing MORE argv
    tokens than this parser's single-token view would ever see."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", ["! ls", "ls; ! rm -rf /tmp/x"])
def test_negation_stays_rejected(text: str) -> None:
    """Tier 1: the third closed bypass shape — a leading ``!`` is the
    shell's NEGATION operator, not a command name; the real command after
    it must not ride along unseen by any future tool-axis policy."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


@pytest.mark.parametrize("text", ["ls\nrm -rf /tmp/x", "echo a\necho b"])
def test_literal_newline_stays_rejected(text: str) -> None:
    """Tier 1: the fourth and worst closed bypass shape — ``shlex`` folds
    a newline into ordinary whitespace, so an unwidened check would parse
    a genuinely two-command input as one, unremarkable-looking segment."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)
