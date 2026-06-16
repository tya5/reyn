"""Tier 2: OS invariant — Tier-1 swebench delegation + binary-sharpening.

Pins the C7 PR3 invariants (FP-0008 C7):

1.  test_model_patch_extraction_real_repo    — real git repo + modify → git diff HEAD
2.  test_model_patch_extraction_empty        — no changes → empty diff string
3.  test_prediction_dict_shape               — build_swebench_prediction returns correct shape
4.  test_accounting_harness_resolved_wins    — harness_resolved=T + tests_passed=F → counted as PASS
5.  test_accounting_harness_resolved_false   — harness_resolved=F + tests_passed=T → counted as FAIL
6.  test_accounting_tests_passed_fallback    — no harness_resolved → tests_passed used (Tier-2/3)
7.  test_accounting_mixed_tier1_and_tier2    — mix of harness_resolved and tests_passed results
8.  test_accounting_skipped_excluded_harness — skipped result with harness_resolved excluded
9.  test_swebench_missing_honest_skip        — swebench ImportError → RuntimeError('swebench_missing')
10. test_lazy_import_not_at_module_top       — swebench NOT in sys.modules after module import
11. test_tier1_routing_structure             — docker_available=True → classify returns 'docker'
12. test_make_verify_skip_record_tier1       — _make_verify_skip_record with _TIER_DOCKER

No mocks (``unittest.mock`` / ``AsyncMock`` / ``MagicMock`` / ``patch``).
Tests that require Docker or swebench-installed are NOT included here — those
are e2e tests validated separately on a Docker host.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tier1_pass(instance_id: str) -> dict:
    """Tier-1 faithfully-verified passing result (harness_resolved=True)."""
    return {
        "instance_id": instance_id,
        "verify_tier": "docker",
        "verify_skipped": False,
        "harness_resolved": True,
        # skill self-check says False — must be overridden by harness_resolved
        "tests_passed": False,
    }


def _make_tier1_fail(instance_id: str) -> dict:
    """Tier-1 faithfully-verified failing result (harness_resolved=False)."""
    return {
        "instance_id": instance_id,
        "verify_tier": "docker",
        "verify_skipped": False,
        "harness_resolved": False,
        # skill self-check says True — must be overridden by harness_resolved
        "tests_passed": True,
    }


def _make_tier2_pass(instance_id: str) -> dict:
    """Tier-2/3 result with only tests_passed (no harness_resolved)."""
    return {
        "instance_id": instance_id,
        "verify_tier": "linux_host",
        "verify_skipped": False,
        "tests_passed": True,
    }


def _make_tier2_fail(instance_id: str) -> dict:
    """Tier-2/3 result with only tests_passed=False (no harness_resolved)."""
    return {
        "instance_id": instance_id,
        "verify_tier": "linux_host",
        "verify_skipped": False,
        "tests_passed": False,
    }


def _make_skipped_with_harness(instance_id: str) -> dict:
    """Verify-skipped result that also carries harness_resolved (must be excluded)."""
    return {
        "instance_id": instance_id,
        "verify_tier": "docker",
        "verify_skipped": True,
        "verify_skip_reason": "Tier1 eval error: test",
        "harness_resolved": True,  # must NOT count — skipped overrides
    }


# ── 1-2: extract_model_patch ──────────────────────────────────────────────────


def test_model_patch_extraction_real_repo(tmp_path) -> None:
    """Tier 2: extract_model_patch returns git diff HEAD from a real modified repo.

    Uses a real git repository (not a mock) to verify the extraction.
    The diff must contain the expected change and be non-empty.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import extract_model_patch

    # Set up a real git repo
    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    # Create and commit a base file
    (tmp_path / "hello.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    # Simulate AI's solution: modify the file
    (tmp_path / "hello.py").write_text(
        'def hello():\n    return "world"\n', encoding="utf-8"
    )

    patch = extract_model_patch(tmp_path)

    assert isinstance(patch, str), "model_patch must be a string"
    assert len(patch) > 0, "model_patch must be non-empty after modification"
    # The diff must reference the changed file and the new content
    assert "hello.py" in patch
    assert 'return "world"' in patch or "+    return" in patch


def test_model_patch_extraction_empty(tmp_path) -> None:
    """Tier 2: extract_model_patch returns empty string when there are no changes.

    git diff HEAD exits 0 with empty output when no files are modified.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import extract_model_patch

    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "unchanged.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    patch = extract_model_patch(tmp_path)

    assert patch == "", f"Expected empty diff when no changes, got: {patch!r}"


# ── 3: build_swebench_prediction ─────────────────────────────────────────────


def test_prediction_dict_shape() -> None:
    """Tier 2: build_swebench_prediction returns the correct swebench prediction shape.

    swebench uses KEY_INSTANCE_ID='instance_id', KEY_MODEL='model_name_or_path',
    KEY_PREDICTION='model_patch'.  The prediction dict must match exactly.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import build_swebench_prediction

    patch = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
    pred = build_swebench_prediction("django__django-12345", patch)

    # Verify the exact keys swebench expects
    assert pred["instance_id"] == "django__django-12345"
    assert pred["model_name_or_path"] == "reyn"
    assert pred["model_patch"] == patch

    # No extra keys that would confuse swebench
    assert set(pred.keys()) == {"instance_id", "model_name_or_path", "model_patch"}


# ── 4-8: compute_faithful_accounting binary-sharpening ───────────────────────


def test_accounting_harness_resolved_wins() -> None:
    """Tier 2: harness_resolved=True + tests_passed=False → counted as PASS.

    THE regression guard: C7 pass-rate must follow the authoritative harness
    verdict (harness_resolved), NOT the skill self-check (tests_passed).
    When both fields are present on a faithful result, harness_resolved wins.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import compute_faithful_accounting

    results = [_make_tier1_pass("t1"), _make_tier1_fail("t2")]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_verified_count"] == 2
    assert acct["faithful_passed"] == 1, (
        "Expected 1 pass from harness_resolved (True+False), "
        f"NOT from tests_passed (False+True); got {acct['faithful_passed']}"
    )
    assert abs(acct["faithful_pass_rate"] - 0.5) < 1e-9, (
        f"Expected pass_rate=0.5 following harness_resolved, "
        f"got {acct['faithful_pass_rate']}"
    )


def test_accounting_harness_resolved_false() -> None:
    """Tier 2: harness_resolved=False + tests_passed=True → counted as FAIL.

    Harness verdict trumps skill self-check even when the self-check claims pass.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import compute_faithful_accounting

    results = [_make_tier1_fail("t1")]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_passed"] == 0, (
        "harness_resolved=False must count as fail even when tests_passed=True; "
        f"got faithful_passed={acct['faithful_passed']}"
    )
    assert acct["faithful_pass_rate"] == 0.0


def test_accounting_tests_passed_fallback() -> None:
    """Tier 2: when no harness_resolved present, tests_passed is used (Tier-2/3 path).

    The fallback path (Tier-2/3 results without harness_resolved) still works
    correctly using tests_passed.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import compute_faithful_accounting

    results = [_make_tier2_pass("t1"), _make_tier2_fail("t2")]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_passed"] == 1
    assert abs(acct["faithful_pass_rate"] - 0.5) < 1e-9


def test_accounting_mixed_tier1_and_tier2() -> None:
    """Tier 2: mix of Tier-1 (harness_resolved) and Tier-2/3 (tests_passed) results.

    Each result uses its own verdict source.
    Tier-1 result with harness_resolved=True contributes a pass.
    Tier-2 result with tests_passed=True contributes a pass.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import compute_faithful_accounting

    results = [
        _make_tier1_pass("t1"),   # harness_resolved=True → PASS
        _make_tier1_fail("t2"),   # harness_resolved=False → FAIL
        _make_tier2_pass("t3"),   # tests_passed=True → PASS
        _make_tier2_fail("t4"),   # tests_passed=False → FAIL
    ]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_verified_count"] == 4
    assert acct["faithful_passed"] == 2
    assert abs(acct["faithful_pass_rate"] - 0.5) < 1e-9


def test_accounting_skipped_excluded_harness() -> None:
    """Tier 2: a verify_skipped result with harness_resolved is excluded from accounting.

    The invariant: NEVER count a verify_skipped result as pass or fail,
    even if it carries a harness_resolved field.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import compute_faithful_accounting

    results = [_make_skipped_with_harness("s1")]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_verified_count"] == 0
    assert acct["faithful_passed"] is None
    assert acct["faithful_pass_rate"] is None
    assert acct["skip_count"] == 1


# ── 9: swebench-missing → honest-skip ────────────────────────────────────────


def test_swebench_missing_honest_skip() -> None:
    """Tier 2: run_tier1_swebench_eval raises RuntimeError('swebench_missing')
    when swebench is not installed.

    The harness converts this to verify_skipped with _TIER1_SWEBENCH_MISSING_REASON.
    NEVER emits fake PASS/FAIL — honest-skip is the only valid outcome when swebench
    is unavailable.

    This test uses a lightweight injectable: since swebench is not installed in
    the CI environment, the real lazy import raises ImportError and the function
    raises RuntimeError('swebench_missing') — no mocks needed.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import (
        _TIER1_SWEBENCH_MISSING_REASON,
        run_tier1_swebench_eval,
    )

    # Ensure swebench is not currently installed in THIS venv
    # (it's a Tier1-only optional dep, not in reyn's core requirements)
    if "swebench" in sys.modules:
        pytest.skip(
            "swebench is installed in this environment; "
            "the swebench-missing path is only exercisable when it is absent"
        )

    with pytest.raises(RuntimeError) as exc_info:
        run_tier1_swebench_eval("test__test-1", "some diff")

    assert str(exc_info.value) == "swebench_missing", (
        f"Expected RuntimeError('swebench_missing'), got {exc_info.value!r}"
    )

    # Confirm the skip reason string is non-empty and contains key instructions
    assert "pip install swebench" in _TIER1_SWEBENCH_MISSING_REASON
    assert len(_TIER1_SWEBENCH_MISSING_REASON) > 0


# ── 10: lazy-import gating ────────────────────────────────────────────────────


def test_lazy_import_not_at_module_top() -> None:
    """Tier 2: swebench is NOT imported at module top of eval_benchmark.

    After importing eval_benchmark, 'swebench' must not appear in sys.modules.
    This guarantees that reyn's runtime + all non-Tier1 code paths never pay
    the swebench import cost or fail with ImportError if swebench is absent.

    The test imports eval_benchmark fresh in a subprocess to guarantee a clean
    sys.modules state independent of the test runner's import history.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, 'src'); "
                "import reyn.interfaces.cli.commands.eval_benchmark; "
                "swebench_keys = [k for k in sys.modules if 'swebench' in k]; "
                "print('swebench_in_modules:', bool(swebench_keys)); "
                "print('keys:', swebench_keys)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\n{result.stderr}"
    )
    output = result.stdout
    assert "swebench_in_modules: False" in output, (
        f"swebench should NOT be imported at module top of eval_benchmark.\n"
        f"subprocess output: {output!r}"
    )


# ── 11: Tier-1 routing structure ──────────────────────────────────────────────


def test_tier1_routing_structure() -> None:
    """Tier 2: classify_verification_tier with docker_available=True → 'docker'.

    The 'docker' tier is the entry point for Tier-1 swebench delegation.
    Routing invariant: when Docker is available, ALL platforms return 'docker'.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import (
        _TIER_DOCKER,
        classify_verification_tier,
    )

    for platform_sys in ("Darwin", "Linux", "Windows"):
        result = classify_verification_tier(
            docker_available=True, platform_system=platform_sys
        )
        assert result == _TIER_DOCKER, (
            f"Expected 'docker' when docker_available=True on {platform_sys}, "
            f"got {result!r}"
        )


# ── 12: _make_verify_skip_record with TIER_DOCKER ─────────────────────────────


def test_make_verify_skip_record_tier1() -> None:
    """Tier 2: _make_verify_skip_record with _TIER_DOCKER produces correct fields.

    When Tier-1 eval fails (swebench missing or docker error), the result must
    be marked as verify_skipped=True with verify_tier='docker' and a non-empty
    reason.  This is the honest-skip invariant for Tier-1.
    """
    from reyn.interfaces.cli.commands.eval_benchmark import (
        _TIER1_SWEBENCH_MISSING_REASON,
        _TIER_DOCKER,
        _make_verify_skip_record,
    )

    record = _make_verify_skip_record(_TIER_DOCKER, _TIER1_SWEBENCH_MISSING_REASON)

    assert record["verify_skipped"] is True
    assert record["verify_tier"] == _TIER_DOCKER
    assert record["verify_skip_reason"] == _TIER1_SWEBENCH_MISSING_REASON
    assert len(record["verify_skip_reason"]) > 0
    # Must NOT contain any verdict fields — no fake PASS/FAIL
    assert "harness_resolved" not in record
    assert "tests_passed" not in record
