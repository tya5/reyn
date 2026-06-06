"""Tier 2: OS/skill invariant — swe_bench verify Step 1 distinguishes an
already-applied test_patch (idempotency rc≠0) from a genuine apply failure
(#1206, observed in astropy-13977).

Primary evidence backing (13977 faithful in-container run, from #1206):
  git apply --3way --recount --whitespace=fix .reyn/swe_bench_test.patch  rc=0  ← APPLIED
  git apply --3way --recount --whitespace=fix .reyn/swe_bench_test.patch  rc=1  ← re-applied → "patch does not apply" (ALREADY applied)
  git checkout HEAD -- .../test_quantity.py                               rc=0  ← reverted
  pytest NEVER run.
The model applied the test_patch (rc=0), re-applied it, read the second
rc=1 ("patch does not apply" = idempotency = already applied) as an apply
*failure*, recorded tests_passed=false + a scary failure_summary, reverted,
and gave up — so a possibly-correct fix was scored FAIL because the tests
were never run.

The fix (verify.md Step 1) makes the model idempotency-aware: apply once;
on a non-zero apply, FIRST reverse-check (`git apply --reverse --check`) —
rc=0 ⇒ already applied ⇒ proceed to Step 2; only neither-applicable-nor-
already-applied is a genuine verify-execution failure.

Two layers are pinned here:
  (a) text-presence invariant — verify.md Step 1 actually carries the
      idempotency distinction (the prompt-side fix exists);
  (b) **idiom-correctness** — the `git apply --reverse --check` idiom the
      instruction relies on is a CORRECT classifier: it returns 0 on an
      already-applied patch and non-zero on a genuine-fail patch. This is
      the load-bearing premise of the fix, verified deterministically at
      the git level WITHOUT an LLM (the right form of the acceptance's
      "fixture exercising the double-apply path"; a recorded LLM-replay
      would be tautological for a pure prompt fix). The model-following-
      the-instruction layer is behavioral = a separate light dogfood.

No mocks. No private-state assertions. (a) reads the on-disk skill file;
(b) drives a throwaway git repo via subprocess.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_VERIFY_MD = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench" / "phases" / "verify.md"
)


# ── (a) text-presence invariant ──────────────────────────────────────────


def test_verify_md_step1_carries_idempotency_distinction() -> None:
    """Tier 2: verify.md Step 1 distinguishes already-applied from apply-failure."""
    text = _VERIFY_MD.read_text(encoding="utf-8")
    # The reverse-check idiom must be present (the deterministic classifier the
    # instruction tells the model to use before declaring an apply failure).
    assert "--reverse --check" in text, (
        "verify.md Step 1 must instruct a reverse-check to detect an "
        "already-applied test_patch before classifying a non-zero apply as a "
        "failure (#1206 idempotency gap)."
    )
    # The already-applied → proceed (NOT fail) semantics must be stated.
    lowered = text.lower()
    assert "already applied" in lowered or "already in the tree" in lowered, (
        "verify.md must name the already-applied case explicitly."
    )
    assert "proceed to step 2" in lowered, (
        "verify.md must tell the model to proceed to running the tests when the "
        "patch is already applied, not set tests_passed=false."
    )


def test_verify_md_step1_forbids_reapply() -> None:
    """Tier 2: verify.md Step 1 unambiguously forbids re-applying (9th-defect driver).

    The 9th-defect driver (astropy-13398/13453) was the model re-applying
    ``git apply`` and misreading the second rc=1 (idempotency) as a failure,
    looping. The minimized Step 1 targets that driver by making non-re-application
    explicit — apply exactly once; on a non-zero apply, reverse-check ONCE, never
    re-apply — so a model following it cannot enter the re-apply loop. Pin that the
    apply-once / do-not-re-apply instruction is present (the prompt-side driver fix
    exists). Behavioural driver-removal is verified by the 13398 faithful re-run,
    not here (prose-presence is necessary, not sufficient).
    """
    lowered = _VERIFY_MD.read_text(encoding="utf-8").lower()
    assert "exactly once" in lowered, (
        "verify.md Step 1 must instruct applying the test_patch exactly once."
    )
    assert "do not re-apply" in lowered or "never re-apply" in lowered, (
        "verify.md Step 1 must explicitly forbid re-applying git apply — the "
        "9th-defect re-apply→misread-rc=1 loop driver."
    )


def test_verify_md_guards_against_apply_equals_pass() -> None:
    """Tier 2: verify.md guards against the 'apply = pass' false-pass shortcut.

    sandbox_2's behavioural N=3 on the minimized verify.md found the model
    reporting tests_passed=true with pytest run 0 times — apply-success conflated
    with test-pass, Step 2 (run the tests) skipped. The minimization had over-cut
    the anti-false-pass guard, and removing the apply-loop exposed the latent
    shortcut. The minimal guard must be present: applying the test_patch is NOT a
    pass on its own, and tests_passed=true requires an actual test run. Behavioural
    removal of the false-pass is confirmed by the 13398 faithful re-run, not here.
    """
    lowered = _VERIFY_MD.read_text(encoding="utf-8").lower()
    assert "not a pass on its own" in lowered, (
        "verify.md must state that applying the test_patch is NOT a pass on its "
        "own (the 'apply = pass' false-pass guard)."
    )
    assert "actually ran the tests" in lowered or "pytest exited 0" in lowered, (
        "verify.md must require actually running the tests (real pytest exit) "
        "before reporting tests_passed=true — not infer a pass from apply success."
    )


# ── (b) idiom-correctness (deterministic, no LLM) ────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "foo.txt").write_text("line1\n", encoding="utf-8")
    _git(r, "add", "foo.txt")
    _git(r, "commit", "-qm", "base")
    return r


# A patch that cleanly applies to the committed foo.txt (adds a line).
_APPLICABLE_PATCH = (
    "--- a/foo.txt\n"
    "+++ b/foo.txt\n"
    "@@ -1 +1,2 @@\n"
    " line1\n"
    "+added line\n"
)

# A patch whose context does NOT match foo.txt — neither applies forward nor
# reverse-applies (a genuine apply failure, distinct from already-applied).
_GENUINE_FAIL_PATCH = (
    "--- a/foo.txt\n"
    "+++ b/foo.txt\n"
    "@@ -1 +1,2 @@\n"
    " nonexistent context line\n"
    "+added line\n"
)


def test_reverse_check_classifies_already_applied_vs_genuine_fail(repo: Path) -> None:
    """Tier 2: `git apply --reverse --check` is a correct already-applied classifier.

    This is the load-bearing premise of the #1206 fix — exercised exactly along
    the double-apply path the model hit in 13977, deterministically, no LLM.
    """
    patch = repo / "test.patch"
    patch.write_text(_APPLICABLE_PATCH, encoding="utf-8")

    # 1. First apply succeeds (rc=0).
    first = _git(repo, "apply", str(patch))
    assert first.returncode == 0, f"first apply should succeed: {first.stderr}"

    # 2. Re-apply fails (rc != 0) — idempotency, NOT a real failure. This is the
    #    exact second-apply the model misread as an apply failure in 13977.
    second = _git(repo, "apply", str(patch))
    assert second.returncode != 0, "re-applying an already-applied patch must fail"

    # 3. The fix's idiom: reverse-check on the already-applied patch SUCCEEDS
    #    (rc=0) → classifies it as already-applied → model should proceed to Step 2.
    rev_applied = _git(repo, "apply", "--reverse", "--check", str(patch))
    assert rev_applied.returncode == 0, (
        "reverse-check must succeed (rc=0) on an already-applied patch — this is "
        f"how the fix detects 'already applied'. stderr={rev_applied.stderr}"
    )

    # 4. A genuine-fail patch (context not in the tree) reverse-checks NON-zero,
    #    so the idiom does NOT mis-classify a real failure as already-applied.
    genuine = repo / "genuine.patch"
    genuine.write_text(_GENUINE_FAIL_PATCH, encoding="utf-8")
    assert _git(repo, "apply", str(genuine)).returncode != 0, (
        "the genuine-fail patch must not apply forward"
    )
    rev_genuine = _git(repo, "apply", "--reverse", "--check", str(genuine))
    assert rev_genuine.returncode != 0, (
        "reverse-check must return non-zero on a genuine-fail patch — otherwise "
        "the fix would mis-classify a real apply failure as already-applied."
    )
