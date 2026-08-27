"""Tier 1: #5314 — a PR body's blocking checkbox must not be closable by
editing the one place the gate looks (the body itself).

#5311/#5312 (one night, 2026-08-27) measured 3 real bypasses of the
original #5135 gate (body-only, open-checkbox-only): a reviewer's
`- [ ] 🔴` deleted in a body rewrite (still unresolved, green), the same
checkbox ticked to `[x]` with no other change (still unresolved, green),
and 4 checkboxes bulk-ticked with genuinely resolved content but nothing
corroborating that (accidentally correct this time, but unverifiable).

Condition A (comment) and condition B (body) are tested both separately
(so a failure names which condition fired) and their OR (via the fixture
CLI, mirroring `evaluate`'s own combined return)."""
from __future__ import annotations

import json

from scripts.check_open_blocking_checkboxes import evaluate, main, run_gate

_HEAD = "abc1234567"


def _pr(body: str, comments: "list[str]" = (), head: str = _HEAD) -> dict:
    return {
        "body": body,
        "comments": [{"body": c} for c in comments],
        "headRefOid": head,
    }


# ---------------------------------------------------------------------------
# Condition B — body (① open, extended ②③④ for checked)
# ---------------------------------------------------------------------------


def test_open_blocking_checkbox_variants_fail() -> None:
    """Tier 1: ① an open checkbox fails, unchanged from #5135 — every
    supported list-marker/formatting variant."""
    for body in (
        "- [ ] 🔴 unresolved",
        "  - [ ] 🔴 nested",
        "* [ ]🔴 compact",
        "+ [ ] **🔴** decorated",
    ):
        code, _ = evaluate(_pr(body))
        assert code != 0


def test_checked_checkbox_with_verbatim_comment_passes() -> None:
    """Tier 1: ② a checked checkbox WITH a comment quoting its own text
    verbatim passes — ticking is not enough on its own, but ticking PLUS
    a corroborating comment is what #5314 asks authors to do."""
    code, _ = evaluate(_pr(
        "- [x] 🔴 the widget must validate input",
        comments=["Fixed — the widget must validate input, confirmed on every call."],
    ))
    assert code == 0


def test_checked_checkbox_with_no_comment_fails() -> None:
    """Tier 1: ③ (the tick bypass, #5311 case 2) — a checked checkbox with
    NO comment anywhere fails. Ticking alone must not be a record."""
    code, lines = evaluate(_pr("- [x] 🔴 the widget must validate input"))
    assert code != 0
    assert any("checked blocking line" in line for line in lines)


def test_checked_checkbox_with_unrelated_comment_fails() -> None:
    """Tier 1: ④ a comment exists, but it does not quote the checked
    line's own text — "something was written" must not be enough, or
    this becomes a checklist item answered yes every time."""
    code, _ = evaluate(_pr(
        "- [x] 🔴 the widget must validate input",
        comments=["Pushed a fix for an unrelated typo in the README."],
    ))
    assert code != 0


# ---------------------------------------------------------------------------
# Condition A — comment (⑤ BLOCKING with no CLEARED, current-head check)
# ---------------------------------------------------------------------------


def test_blocking_comment_with_no_cleared_fails_even_with_empty_body() -> None:
    """Tier 1: ⑤ a BLOCKING comment with no BLOCKING-CLEARED anywhere
    fails — even when the body has nothing in it at all (the reviewer
    who follows house rule 7 and posts the point as a comment is
    protected regardless of what the body currently says)."""
    code, lines = evaluate(_pr(
        "",
        comments=[f"**[architect]** — BLOCKING (head {_HEAD})\nThe cache must be invalidated on write."],
    ))
    assert code != 0
    assert any("BLOCKING comment has no matching" in line for line in lines)


def test_blocking_comment_with_verbatim_cleared_at_current_head_passes() -> None:
    """Tier 1: a BLOCKING comment with a LATER BLOCKING-CLEARED comment
    that names the current head and quotes the identifying line verbatim
    passes — the two-comment record #5314 asks reviewers to keep."""
    code, _ = evaluate(_pr(
        "",
        comments=[
            "**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write.",
            f"**[architect]** — BLOCKING-CLEARED (head {_HEAD})\nThe cache must be invalidated on write. Now handled in commit {_HEAD}.",
        ],
    ))
    assert code == 0


def test_cleared_naming_a_stale_head_does_not_resolve() -> None:
    """Tier 1: (architect's addition) a BLOCKING-CLEARED that names an
    OLDER head than the PR's current one does not resolve it: a push
    landing after the pair reopens the point (mirrors
    check_tests_read_names_its_tree.py's own ∃-over-CURRENT-head rule)."""
    code, lines = evaluate(_pr(
        "",
        comments=[
            "**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write.",
            "**[architect]** — BLOCKING-CLEARED (head 1111111)\nThe cache must be invalidated on write. Now handled.",
        ],
        head="2222222",  # a new push landed after the pair
    ))
    assert code != 0
    assert any("current head" in line for line in lines)


def test_cleared_with_different_wording_does_not_resolve() -> None:
    """Tier 1: a CLEARED comment that exists but does not quote the
    BLOCKING comment's identifying line verbatim does not resolve it —
    "some comment was posted" is not "this point was addressed"."""
    code, _ = evaluate(_pr(
        "",
        comments=[
            "**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write.",
            f"**[architect]** — BLOCKING-CLEARED (head {_HEAD})\nLooks good to me, thanks!",
        ],
    ))
    assert code != 0


def test_blocking_survives_a_full_body_deletion_when_never_cleared() -> None:
    """Tier 1: ⑥ (the deletion bypass, #5311 case 1 — "本命") — a BLOCKING
    comment that was NEVER cleared stays red even after the body's own
    copy of the point is deleted entirely (body="") — condition A never
    reads the body, so there is nothing there to delete out from under
    it."""
    code, lines = evaluate(_pr(
        "",  # the reviewer's original checkbox line has been deleted from the body
        comments=[f"**[architect]** — BLOCKING (head {_HEAD})\nThe cache must be invalidated on write."],
    ))
    assert code != 0
    assert any("BLOCKING comment has no matching" in line for line in lines)


# ---------------------------------------------------------------------------
# Fail-closed / CLI plumbing (unchanged shape from #5135)
# ---------------------------------------------------------------------------


def test_missing_body_fails_closed() -> None:
    """Tier 1: a missing PR body fails closed rather than passing vacuously."""
    code, _ = evaluate({"comments": [], "headRefOid": _HEAD})
    assert code != 0


def test_pr_supplier_failure_fails_closed() -> None:
    """Tier 1: a failed PR-payload supplier returns nonzero without patching."""
    def fail():
        raise OSError("network down")

    assert run_gate(fail) != 0


def test_fixture_cli_supports_both_states(tmp_path) -> None:
    """Tier 1: the CLI reports both a resolved and an unresolved fixture."""
    fixture = tmp_path / "pr.json"
    fixture.write_text(
        json.dumps(_pr(
            "- [x] 🔴 done",
            comments=["Fixed — done."],
        )),
        encoding="utf-8",
    )
    assert main(["--fixture", str(fixture)]) == 0
    fixture.write_text(json.dumps(_pr("- [ ] 🔴 todo")), encoding="utf-8")
    assert main(["--fixture", str(fixture)]) != 0
