"""Tier 1: `scripts/check_tests_read_names_its_tree.py`'s own contract — a
`tests/`-touching PR whose TESTS-READ note does not name the tree it read, or
names one `tests/` has since moved past, is rejected.

Contract, not OS invariant: the subject is a repo gate's decision function, and
what it protects is house rule 8's blind spot (the rule is satisfied by a note
EXISTING; nothing looks at which tree the note is a claim about). The four
2026-08-22 instances the script's own docstring lists are the population this
was written against — the reject-side cases below are those instances' shape.

Payloads are built inline rather than fetched: `evaluate` is pure by design so
the decision is testable without GitHub, and the fixtures here are the only
callers that need to exist.
"""
from __future__ import annotations

import importlib.util

from tests._support.paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "_check_tests_read",
    REPO_ROOT / "scripts" / "check_tests_read_names_its_tree.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)


def _pr(*, files, comments, commits, head="ffffffff"):
    return {"files": files, "comments": comments, "commits": commits, "headRefOid": head}


def _commit(oid, headline="", tests=()):
    return {"oid": oid, "messageHeadline": headline, "_tests_paths": list(tests)}


# ── reject side ─────────────────────────────────────────────────────────────

def test_note_without_a_sha_is_rejected():
    """Tier 1: the #5096 shape — a note landed, but nothing in it says which tree."""
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "**[docs-maintainer]** — TESTS-READ (B: independent). Passing."}],
        commits=[_commit("aaaaaaaa", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",))],
    ))
    assert code == 1
    assert "names a commit of" in "\n".join(lines)


def test_note_whose_sha_predates_a_later_tests_commit_is_rejected():
    """Tier 1: the #5090 / #5095 shape — the note is real and names its head, and then
    `tests/` moved. The failure names the commits so the ask is a differential
    top-up, not a re-read."""
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "TESTS-READ (B: independent) (head `a7c44eef6`)."}],
        commits=[
            _commit("a7c44eef6", "docs: split the column"),
            _commit("9ef30f95b", "test(#4206): CI-gate the Reload column too", ("tests/repo/test_config_reference_declared_in_4206.py",)),
        ],
    ))
    assert code == 1
    joined = "\n".join(lines)
    assert "9ef30f95" in joined, "the offending commit is named, so the ask is scoped"
    assert "DIFFERENTIAL" in joined.upper()


def test_tests_touching_pr_with_no_note_at_all_is_rejected():
    """Tier 1: rule 8 itself — nothing has been checking it mechanically."""
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "LGTM"}],
        commits=[_commit("aaaaaaaa", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",))],
    ))
    assert code == 1
    assert "no TESTS-READ note" in "\n".join(lines)


# ── accept side ─────────────────────────────────────────────────────────────

def test_note_naming_the_last_tests_commit_passes():
    """Tier 1: the accept side — a note that names the newest tests/ commit is
    a claim about the tree that is about to merge."""
    code, _ = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "TESTS-READ (B) (head `bbbbbbbb`)"}],
        commits=[
            _commit("aaaaaaaa", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",)),
            _commit("bbbbbbbb", "test: fix", ("tests/scripts/test_check_doc_drift_5003.py",)),
        ],
    ))
    assert code == 0


def test_commits_after_the_note_that_do_not_touch_tests_pass():
    """Tier 1: a docs-only or src-only commit after the note does not
    invalidate it —
    the note is a claim about `tests/`, so only `tests/` moving falsifies it."""
    code, _ = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "TESTS-READ (B) (head `aaaaaaaa`)"}],
        commits=[
            _commit("aaaaaaaa", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",)),
            _commit("cccccccc", "docs: reword"),
        ],
    ))
    assert code == 0


def test_pr_not_touching_tests_is_out_of_scope():
    """Tier 1: rule 8 binds only PRs that touch tests/ — a src- or docs-only PR
    must not be asked for a note it does not owe."""
    code, lines = _MOD.evaluate(_pr(
        files=["src/reyn/foo.py"], comments=[], commits=[_commit("aaaaaaaa", "fix")],
    ))
    assert code == 0
    assert "does not touch tests/" in "\n".join(lines)


def test_an_all_hex_english_word_is_not_read_as_a_sha():
    """Tier 1: `decade`/`faceted` and friends match a bare hex pattern. Without the
    false-friend filter a note saying "a decade of drift" would be read as
    naming a tree, and this gate would pass a note that names nothing."""
    oids = ["a7c44eef6"]
    assert _MOD.find_note_shas("a decade of drift, faceted", oids) == []
    assert _MOD.find_note_shas("head `a7c44eef6` here", oids) == ["a7c44eef6"]


def test_an_issue_comment_id_is_not_read_as_a_sha():
    """Tier 1: the gate's own first live run failed this way — run against PR #5090 it
    read `5379476813` — an issue-comment id, ten decimal digits, and decimal
    digits are hex digits — as the tree the note named, and passed a PR whose
    note predated two later `tests/` commits.

    Membership in the PR's own commit list is what rejects it; a hex SHAPE
    cannot."""
    oids = ["a7c44eef6", "9ef30f95b"]
    assert _MOD.find_note_shas("see issuecomment-5379476813", oids) == []
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "TESTS-READ (B) — details in issuecomment-5379476813"}],
        commits=[
            _commit("a7c44eef6", "docs: split"),
            _commit("9ef30f95b", "test: gate the column", ("tests/repo/test_config_reference_declared_in_4206.py",)),
        ],
    ))
    assert code == 1, "a note citing only a comment id names no tree"


def test_a_commit_this_checkout_cannot_resolve_is_not_reported_as_clean():
    """Tier 1: `fetch_pr`'s other half of the same failure — `git show <oid>` on an
    unfetched or squashed commit exits non-zero, and treating its (empty)
    output as "touched no tests" is a green computed from commits the checkout
    never read. The live run hit this too — the PR passed while its real
    `tests/` commits were invisible."""
    import subprocess as _sp

    calls: "list[list[str]]" = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["gh", "pr"]:
            return _sp.CompletedProcess(
                cmd, 0,
                stdout='{"files":[{"path":"tests/scripts/test_check_doc_drift_5003.py"}],"comments":[],'
                       '"commits":[{"oid":"deadbee1","messageHeadline":"x"}],'
                       '"headRefOid":"deadbee1"}',
            )
        return _sp.CompletedProcess(cmd, 128, stdout="", stderr="bad object")

    real_run = _MOD.subprocess.run
    _MOD.subprocess.run = _fake_run
    try:
        raised = False
        try:
            _MOD.fetch_pr(5090)
        except SystemExit as exc:
            raised = True
            assert "cannot resolve commit" in str(exc)
        assert raised, "an unresolvable commit must stop the run, not read as clean"
    finally:
        _MOD.subprocess.run = real_run
