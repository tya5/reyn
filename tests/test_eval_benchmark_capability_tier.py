"""Tier 2: OS invariant — benchmark capability-detection + honest-skip + faithful-PASS-rate.

Pins the C7 PR1 invariants (FP-0008 C7):

1. test_classify_tier_docker         — docker_available=True → "docker"
2. test_classify_tier_linux_host     — docker=False + Linux → "linux_host"
3. test_classify_tier_no_faithful_darwin  — docker=False + Darwin → "no_faithful_env"
4. test_classify_tier_no_faithful_windows — docker=False + Windows → "no_faithful_env"
5. test_accounting_faithful_only     — pass_rate computed over faithful subset only
6. test_accounting_skip_excluded     — verify_skipped result excluded from rate
7. test_accounting_skip_count        — skip_count matches skipped result count
8. test_accounting_empty_faithful    — pass_rate null when 0 faithful results
9. test_honest_skip_fields           — no_faithful_env path → verify_skipped=True + reason + tier
10. test_honest_skip_reason_nonempty — verify_skip_reason is non-empty string
11. test_honest_skip_tier_set        — verify_tier field equals the detected tier

No mocks (``unittest.mock`` / ``AsyncMock`` / ``MagicMock`` / ``patch``).
Pure classifier + real accounting dicts — no subprocess / platform probing.
"""
from __future__ import annotations

import pytest  # noqa: F401 — used for pytest.raises in future tests

from reyn.interfaces.cli.commands.eval_benchmark import (
    _TIER_DOCKER,
    _TIER_LINUX_HOST,
    _TIER_NO_FAITHFUL_ENV,
    _make_verify_skip_record,
    classify_verification_tier,
    compute_faithful_accounting,
)

# ── 1-4: classify_verification_tier (pure, explicit inputs) ──────────────────


def test_classify_tier_docker() -> None:
    """Tier 2: docker_available=True → returns 'docker' regardless of platform."""
    result = classify_verification_tier(docker_available=True, platform_system="Darwin")
    assert result == _TIER_DOCKER

    result2 = classify_verification_tier(docker_available=True, platform_system="Linux")
    assert result2 == _TIER_DOCKER

    result3 = classify_verification_tier(docker_available=True, platform_system="Windows")
    assert result3 == _TIER_DOCKER


def test_classify_tier_linux_host() -> None:
    """Tier 2: docker=False + platform_system='Linux' → returns 'linux_host'."""
    result = classify_verification_tier(docker_available=False, platform_system="Linux")
    assert result == _TIER_LINUX_HOST


def test_classify_tier_no_faithful_darwin() -> None:
    """Tier 2: docker=False + Darwin → returns 'no_faithful_env'."""
    result = classify_verification_tier(docker_available=False, platform_system="Darwin")
    assert result == _TIER_NO_FAITHFUL_ENV


def test_classify_tier_no_faithful_windows() -> None:
    """Tier 2: docker=False + Windows → returns 'no_faithful_env'."""
    result = classify_verification_tier(docker_available=False, platform_system="Windows")
    assert result == _TIER_NO_FAITHFUL_ENV


# ── 5-8: compute_faithful_accounting ─────────────────────────────────────────


def _faithful_pass(instance_id: str) -> dict:
    """Build a faithfully-verified passing result record."""
    return {
        "instance_id": instance_id,
        "verify_tier": _TIER_DOCKER,
        "verify_skipped": False,
        "tests_passed": True,
    }


def _faithful_fail(instance_id: str) -> dict:
    """Build a faithfully-verified failing result record."""
    return {
        "instance_id": instance_id,
        "verify_tier": _TIER_DOCKER,
        "verify_skipped": False,
        "tests_passed": False,
    }


def _skipped(instance_id: str) -> dict:
    """Build a verify-skipped result record (no_faithful_env)."""
    return {
        "instance_id": instance_id,
        "verify_tier": _TIER_NO_FAITHFUL_ENV,
        "verify_skipped": True,
        "verify_skip_reason": "test skip reason",
        # tests_passed from skill's self-check — must NOT count in the rate
        "tests_passed": True,
    }


def test_accounting_faithful_only() -> None:
    """Tier 2: pass_rate is computed over the faithful-verified subset only.

    Given 2 faithful (1 pass, 1 fail) + 1 skipped (has tests_passed=True),
    the pass_rate must be 1/2 = 0.5, NOT 2/3.
    """
    results = [
        _faithful_pass("t1"),
        _faithful_fail("t2"),
        _skipped("t3"),
    ]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_verified_count"] == 2
    assert acct["faithful_passed"] == 1
    assert abs(acct["faithful_pass_rate"] - 0.5) < 1e-9, (
        f"Expected faithful_pass_rate=0.5, got {acct['faithful_pass_rate']}"
    )


def test_accounting_skip_excluded() -> None:
    """Tier 2: a verify_skipped result (even with tests_passed=True) is excluded from rate."""
    # 3 skipped (all claiming tests_passed=True) + 0 faithful
    results = [_skipped(f"s{i}") for i in range(3)]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_verified_count"] == 0
    # No faithful results → pass_rate must be null, not 100%
    assert acct["faithful_passed"] is None, (
        "faithful_passed should be None when there are no faithful results"
    )
    assert acct["faithful_pass_rate"] is None, (
        "faithful_pass_rate should be None when there are no faithful results — "
        "never emit a non-faithful PASS/FAIL"
    )


def test_accounting_skip_count() -> None:
    """Tier 2: skip_count matches the number of results with verify_skipped=True."""
    results = [
        _faithful_pass("t1"),
        _skipped("s1"),
        _skipped("s2"),
        _faithful_fail("t2"),
    ]
    acct = compute_faithful_accounting(results)

    assert acct["skip_count"] == 2
    assert acct["faithful_verified_count"] == 2


def test_accounting_empty_faithful() -> None:
    """Tier 2: pass_rate is null when 0 results are faithful-verified."""
    results = [_skipped("s1"), _skipped("s2")]
    acct = compute_faithful_accounting(results)

    assert acct["faithful_verified_count"] == 0
    assert acct["faithful_pass_rate"] is None
    assert acct["faithful_passed"] is None


# ── 9-11: honest-skip marking (_make_verify_skip_record + no_faithful_env path) ─


def test_honest_skip_fields() -> None:
    """Tier 2: _make_verify_skip_record returns verify_skipped=True + tier + reason fields."""
    record = _make_verify_skip_record(_TIER_NO_FAITHFUL_ENV, "test reason")

    assert record["verify_skipped"] is True
    assert record["verify_tier"] == _TIER_NO_FAITHFUL_ENV
    assert "verify_skip_reason" in record


def test_honest_skip_reason_nonempty() -> None:
    """Tier 2: the no_faithful_env skip reason is a non-empty string."""
    from reyn.interfaces.cli.commands.eval_benchmark import _NO_FAITHFUL_ENV_REASON

    assert isinstance(_NO_FAITHFUL_ENV_REASON, str)
    assert len(_NO_FAITHFUL_ENV_REASON) > 0


def test_honest_skip_tier_set() -> None:
    """Tier 2: verify_tier field in the skip record equals the tier passed in."""
    for tier in (_TIER_DOCKER, _TIER_LINUX_HOST, _TIER_NO_FAITHFUL_ENV):
        record = _make_verify_skip_record(tier, "some reason")
        assert record["verify_tier"] == tier, (
            f"Expected verify_tier={tier!r}, got {record['verify_tier']!r}"
        )


# ── integration: accounting + summary.json contains verify_accounting block ──


def test_write_summary_verify_accounting_block(tmp_path) -> None:
    """Tier 2: _write_summary emits a prominent verify_accounting block in summary.json.

    When all results are verify_skipped=True, pass_rate must be null and
    verify_accounting.verify_skipped must equal the total completed count.
    """
    import json

    from reyn.interfaces.cli.commands.eval_benchmark import _write_summary

    run_dir = tmp_path / "run_c7_test"
    run_dir.mkdir()

    # Simulate 3 tasks all skipped (= macOS without Docker host)
    results = [
        {
            "instance_id": f"t{i}",
            "cost_usd": 0.05,
            "verify_tier": _TIER_NO_FAITHFUL_ENV,
            "verify_skipped": True,
            "verify_skip_reason": "no faithful env",
            "tests_passed": True,  # skill self-check — must not count
        }
        for i in range(3)
    ]

    _write_summary(run_dir, "run_c7_test", "swe_bench", results, total_tasks=3)

    data = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    # Top-level pass_rate must be null (0 faithful verified)
    assert data["pass_rate"] is None, (
        f"pass_rate should be null when all results are verify_skipped; got {data['pass_rate']}"
    )
    assert data["passed"] is None

    # verify_accounting block must be present and prominent
    assert "verify_accounting" in data, "summary.json must contain a 'verify_accounting' block"
    va = data["verify_accounting"]

    assert va["faithful_verified"] == 0
    assert va["verify_skipped"] == 3
    assert va["faithful_pass_rate"] is None
