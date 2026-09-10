"""Tier 1: #5987 stage 2 — wiring `derive_bash_node_kind_names` (stage 1)
into `parse_exec_plan`'s actual classification (lead-coder's staging
ruling, issuecomment-5618328297).

This file covers what stage 1's own test file explicitly did NOT (and
could not) witness yet — `test_5987_bash_node_kinds_stage1.py`'s own
docstring says stage 1 "wires NOTHING into `parse_exec_plan`'s
classification". The pre-existing `test_5838_exec_plan_parser.py` is the
before/after witness that the SAME 4 previously character-caught bypass
shapes (a literal newline, `!` negation, `$`/glob/`~` expansion, brace
`{a,b}` expansion) still reject via the NEW node-kind path — this file
adds what is genuinely NEW to stage 2 and has no prior test:

- The allowlist's own population is verified against stage 1's real,
  derived grammar output (never a hand-typed string trusted on faith).
- A construct the character-only parser had NO way to see at all (a
  shell control structure) is now rejected — a real capability gap the
  unit swap closes, not merely a re-routing of an existing rejection.
"""
from __future__ import annotations

import pytest

from reyn.security.bash_node_kinds import derive_bash_node_kind_names
from reyn.security.exec_plan import (
    _ALLOWED_NODE_KINDS,
    ExecPlanRejected,
    parse_exec_plan,
)


def test_every_allowed_node_kind_is_a_real_derived_grammar_kind() -> None:
    """Tier 1: every string in :data:`_ALLOWED_NODE_KINDS` must be a
    member of stage 1's :func:`derive_bash_node_kind_names` — the real,
    current ``tree-sitter-bash`` grammar's own node-kind population, read
    from the INSTALLED package. Catches a hand-typed allowlist entry that
    does not (or no longer, after some future dependency bump) exist in
    the real grammar — the exact drift stage 1's own docstring names as
    the reason to derive rather than hand-enumerate."""
    derived = derive_bash_node_kind_names()
    missing = _ALLOWED_NODE_KINDS - derived
    assert not missing, (
        f"_ALLOWED_NODE_KINDS names kind(s) {sorted(missing)!r} that "
        "derive_bash_node_kind_names()'s real, installed-package output "
        "does not contain"
    )


@pytest.mark.parametrize("text", [
    "if true; then echo hi; fi",
    "for i in 1 2; do echo $i; done",
    "while true; do echo hi; done",
    "case x in y) echo hi;; esac",
])
def test_shell_control_structures_are_rejected(text: str) -> None:
    """Tier 1: a real capability gap the node-kind allowlist closes that
    the OLD character-only classification could not — none of `if`/
    `for`/`while`/`case`'s own keyword characters were individually
    forbidden, so the pre-#5987-stage-2 parser would have silently
    accepted these as an ordinary chain of argv segments (e.g. `if`,
    `true` as one segment, `;` as a chain op, ...), never as the control
    structure a real shell actually runs. Rejected now because
    `if_statement`/`for_statement`/`while_statement`/`case_statement`
    are grammar node kinds outside :data:`_ALLOWED_NODE_KINDS` —
    unknown-kind rejection (gap D: reject, not escalate — unchanged)."""
    with pytest.raises(ExecPlanRejected):
        parse_exec_plan(text)


def test_complete_heredoc_still_gets_the_specific_heredoc_reason() -> None:
    """Tier 1: a COMPLETE heredoc (body + terminator, so the tree has NO
    parse error, unlike the pre-existing ``cat << EOF``-without-a-body
    case in ``test_5838_exec_plan_parser.py``) is caught by the
    node-kind-not-allowed branch instead of the has-error branch, and
    still gets the SAME specific "heredoc" reason, not the generic
    "unsupported node kind" message — both branches route through the
    same disclosed :data:`_HEREDOC_NODE_KINDS` special-case."""
    with pytest.raises(ExecPlanRejected, match="heredoc"):
        parse_exec_plan("cat << EOF\nhi\nEOF")
