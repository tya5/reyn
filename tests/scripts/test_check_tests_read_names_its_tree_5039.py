"""Tier 1: `scripts/check_tests_read_names_its_tree.py`'s own contract — a
`tests/`-touching PR whose TESTS-READ note does not name the tree it read, or
names one `tests/` has since moved past, is rejected.

Contract, not OS invariant: the subject is a repo gate's decision function, and
what it protects is house rule 8's blind spot (the rule is satisfied by a note
EXISTING; nothing looks at which tree the note is a claim about). The four
2026-08-22 instances the script's own docstring lists are the population this
was written against — the reject-side cases below are those instances' shape.

The claim line is a PR COMMENT, and only that comment's FIRST LINE is ever
read — the marker and the head SHA must be co-located there (architect,
#5138 comment 5383200442). The tests below cover the architect's four
acceptance cases:

① no claim line anywhere → red (`test_no_claim_line_at_all_is_rejected`).
② a comment whose marker appears only from line 2 onward → red — the #5144
   reproduction, built from that PR's actual shape: prose that discussed
   TESTS-READ and happened to name a real commit of the PR elsewhere in the
   same multi-purpose document, with no first-line claim at all
   (`test_marker_only_from_line_two_onward_is_not_a_claim`).
③ the CI job dying mid-run leaves the posted commit status `pending`, never
   green. This is a property of the THREE-STEP workflow shape in
   `check-tests-read-names-its-tree.yml` (`pending` before the script runs,
   `success`/`failure` after, the final step gated on `if: always()`) — not
   something `evaluate` or any other pure function here can exercise, so it
   is deliberately NOT represented by a test in this file. Recorded here
   rather than left silent: this file's own test-review discipline (six
   questions, #4) treats an assertion that would pass vacuously as worse than
   no assertion, and a Tier-1 test cannot reach into a GitHub Actions run.
④ a claim naming a sha that later tests/-touching commits moved past → red,
   listing those commits (`test_note_whose_sha_predates_a_later_tests_commit_is_rejected`).

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


def _pr(*, files, comments=(), commits, head="ffffffff"):
    return {
        "files": files, "comments": list(comments), "commits": commits,
        "headRefOid": head,
    }


def _commit(oid, headline="", tests=()):
    return {"oid": oid, "messageHeadline": headline, "_tests_paths": list(tests)}


# ── reject side ─────────────────────────────────────────────────────────────

def test_note_without_a_sha_is_rejected():
    """Tier 1: the #5096 shape — a claim line landed on a comment's first
    line, but nothing on it says which tree."""
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
    top-up, not a re-read. (Architect's acceptance ④.)"""
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


def test_no_claim_line_at_all_is_rejected():
    """Tier 1: rule 8 itself — nothing has been checking it mechanically.
    (Architect's acceptance ①.)"""
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "LGTM"}],
        commits=[_commit("aaaaaaaa", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",))],
    ))
    assert code == 1
    assert "no TESTS-READ note" in "\n".join(lines)


def test_marker_only_from_line_two_onward_is_not_a_claim():
    """Tier 1: the #5144 reproduction (architect's acceptance ②) — a
    multi-purpose comment whose FIRST line is a plain role-prefixed opener,
    and whose marker plus a real commit SHA of this PR only appear further
    down, inside prose that is ABOUT TESTS-READ rather than STATING a
    TESTS-READ claim. That is exactly the shape that made #5144's PR BODY
    (a different multi-purpose document) go green with no reviewer note:
    the marker and a real SHA were both present somewhere in the text, just
    not co-located as a claim. This must be red, and — critically — for the
    SAME reason as "no note at all", because the first line carries no
    claim."""
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{
            "body": (
                "**[e2e-coder]** — 着手します。\n"
                "\n"
                "## Notes\n"
                "\n"
                "Reviewed the diff; will post TESTS-READ once done. Fixing "
                "commit was `bbbbbbbb`, same tree this PR already has."
            ),
        }],
        commits=[_commit("bbbbbbbb", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",))],
    ))
    assert code == 1
    assert "no TESTS-READ note" in "\n".join(lines), (
        "a marker appearing only from line 2 onward must fall into the same "
        "bucket as no marker at all -- it must not be read as a claim"
    )


# ── accept side ─────────────────────────────────────────────────────────────

def test_note_naming_the_last_tests_commit_passes():
    """Tier 1: the accept side — a claim on a comment's first line, naming
    the newest tests/ commit, is a claim about the tree that is about to
    merge."""
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


def test_multiline_comment_with_the_claim_on_line_one_still_passes():
    """Tier 1: the first-line restriction rejects a marker BELOW line 1 — it
    must not also reject a well-formed claim that simply has grounds
    written below it, which is the normal, expected shape (marker + SHA on
    line 1, six-questions write-up on lines 2+)."""
    code, _ = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{
            "body": (
                "**[architect]** — TESTS-READ (B: independent) (head `aaaaaaaa`)\n"
                "\n"
                "## Six questions\n"
                "1. Tier 1, the same expression is not on both sides.\n"
            ),
        }],
        commits=[_commit("aaaaaaaa", "test: add", ("tests/scripts/test_check_doc_drift_5003.py",))],
    ))
    assert code == 0


def test_pr_not_touching_tests_is_out_of_scope():
    """Tier 1: rule 8 binds only PRs that touch tests/ — a src- or docs-only PR
    must not be asked for a note it does not owe."""
    code, lines = _MOD.evaluate(_pr(
        files=["src/reyn/foo.py"], commits=[_commit("aaaaaaaa", "fix")],
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
    assert "names a commit of" in "\n".join(lines), (
        "the rejection is for naming no commit, not for a missing note"
    )


def _runner(
    returncode: int, stdout: str = "", stderr: str = "",
    seen: "list | None" = None, kwargs_seen: "list | None" = None,
):
    """A minimal stand-in for `subprocess.run`'s result, injected through
    `commit_touched_paths`'s own `run` seam. Not a mock of a cheaply
    constructible collaborator — the real one is an authenticated network
    call, and using it made the reject case below pass in CI for the wrong
    reason (no token, rather than a bogus commit).

    *seen* collects the argv it was handed. An earlier revision discarded it,
    which left the seam's ONE mistakable part — the endpoint path and the
    ``--jq`` that shapes the reply — outside every test (architect, #5128 B):
    a `commit_touched_paths` that asked for the wrong URL would have passed
    both cases below.

    *kwargs_seen* collects the keyword arguments the caller passed (e.g.
    ``capture_output=``, ``text=``) — recorded, not silently swallowed, so a
    caller that started passing `check=True` or a different `text=` would be
    visible to a test rather than invisible to every test using this seam."""
    import subprocess as _sp

    def _run(cmd, **kwargs):
        if seen is not None:
            seen.append(list(cmd))
        if kwargs_seen is not None:
            kwargs_seen.append(dict(kwargs))
        return _sp.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _run


def test_the_seam_asks_github_for_that_commits_file_names() -> None:
    """Tier 1: the command the seam issues — the part a reader cannot check by
    running the suite unless a test looks at it. Endpoint and `--jq` together
    decide whether the reply is a list of file names at all."""
    seen: list = []
    kwargs_seen: list = []
    _MOD.commit_touched_paths(
        "abc1234", "tya5/reyn",
        run=_runner(0, stdout="[]", seen=seen, kwargs_seen=kwargs_seen),
    )
    # `seen[0]` raises if the seam was never called at all, so the shape
    # assertions below cannot pass vacuously.
    argv = seen[0]
    assert argv[:2] == ["gh", "api"], f"reads via the API, not a checkout; got {argv[:2]!r}"
    assert "repos/tya5/reyn/commits/abc1234" in argv, (
        f"asks for THAT commit in THAT repo; got {argv!r}"
    )
    assert "[.files[].filename]" in argv, (
        f"asks for file NAMES — without this jq the reply is objects, not paths; got {argv!r}"
    )
    # capture_output/text together decide whether stdout comes back as a
    # decoded string this function can json.loads — a caller that dropped
    # either would still issue the right argv and still break at runtime.
    kwargs = kwargs_seen[0]
    assert kwargs.get("capture_output") is True, f"needs captured stdout/stderr; got {kwargs!r}"
    assert kwargs.get("text") is True, f"needs str, not bytes, output; got {kwargs!r}"


def test_an_unreadable_commit_stops_the_run() -> None:
    """Tier 1: an API failure must not read as "touched no tests" — that would
    be a green computed from a commit this check could not read, the shape it
    exists to reject."""
    import pytest

    with pytest.raises(SystemExit) as exc:
        _MOD.commit_touched_paths(
            "0" * 40, "tya5/reyn", run=_runner(1, stderr="Not Found"),
        )
    assert "cannot read commit" in str(exc.value)


def test_a_readable_commit_reports_its_paths() -> None:
    """Tier 1: the same seam's accept side, so the reject case above is not the
    only thing exercised — a `commit_touched_paths` that raised unconditionally
    would still pass the reject test."""
    paths = _MOD.commit_touched_paths(
        "abc1234", "tya5/reyn",
        run=_runner(0, stdout='["src/reyn/runtime/registry.py", "tests/scripts/test_check_doc_drift_5003.py"]'),
    )
    assert paths == [
        "src/reyn/runtime/registry.py",
        "tests/scripts/test_check_doc_drift_5003.py",
    ]


def test_a_note_quoting_the_head_with_no_commits_does_not_pass():
    """Tier 1: the vacuity gap docs-maintainer found in this gate's own B
    (#5120) — `headRefOid` used to be appended to the membership list, so a
    note quoting the head satisfied "names a commit of this PR" while an empty
    commit list left `tests_commits_after` nothing to find. Green with nothing
    read, which is the shape this gate exists to reject."""
    code, lines = _MOD.evaluate(_pr(
        files=["tests/scripts/test_check_doc_drift_5003.py"],
        comments=[{"body": "TESTS-READ (B) (head `deadbee1`)"}],
        commits=[],
        head="deadbee1",
    ))
    assert code == 1
    assert "commit list is empty" in "\n".join(lines)


def test_the_head_alone_is_not_membership():
    """Tier 1: the same gap at the predicate level — a SHA that is only the
    head, with no commit carrying it, is not a commit of this PR."""
    assert _MOD.find_note_shas("head `deadbee1`", []) == []


def test_tests_commits_after_takes_no_head_argument():
    """Tier 1: `tests_commits_after` decides membership from *sha* against
    *commits* alone — a `head` parameter that the decision never consulted
    would claim to participate without doing so (the shape a signature
    should not carry). Its 2-argument form is the contract."""
    later = _MOD.tests_commits_after(
        "aaaaaaaa",
        [
            _commit("aaaaaaaa", "test: add"),
            _commit("bbbbbbbb", "test: more", ("tests/scripts/test_check_doc_drift_5003.py",)),
        ],
    )
    assert [c["oid"] for c in later] == ["bbbbbbbb"]
