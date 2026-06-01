"""Tier 2: OS invariant — swe_bench_runner.py I/O contract.

Pins Component-C wrapper logic (FP-0008 PR-C):

1.  test_parse_input_valid              — valid JSON dict → parsed correctly
2.  test_parse_input_via_stdin_shim     — same logic exercised via string (stdin path)
3.  test_parse_input_missing_field      — missing required field → ValueError
4.  test_parse_input_malformed_json     — malformed JSON → ValueError
5.  test_format_output_success_shape   — patch supplied → correct harness JSON shape
6.  test_format_output_custom_model    — --model-name propagates to output
7.  test_format_output_error_shape     — error supplied → error JSON shape
8.  test_extract_patch_from_marker     — reyn stdout with marker → patch extracted
9.  test_extract_patch_from_nested     — nested data.patch → extracted correctly
10. test_run_reyn_success              — fake reyn (success mode) → ok + patch
11. test_run_reyn_nonzero              — fake reyn exits 1 → error result, ok=False
12. test_run_reyn_bad_output           — fake reyn bad output → error result, ok=False
13. test_run_reyn_timeout              — fake reyn hangs → error result with "timeout"
14. test_main_input_file               — end-to-end via --input file path
15. test_main_stdin_flag               — end-to-end via --stdin

No unittest.mock / AsyncMock / MagicMock / patch.
All subprocess testing uses the fake_reyn_for_swe_bench.py fixture via
direct reyn_cmd injection (Approach A from the FP design).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Path to the fake reyn fixture script
_FIXTURE = Path(__file__).parent / "fixtures" / "fake_reyn_for_swe_bench.py"

# Minimal valid SWE-bench instance
_VALID_INSTANCE = {
    "instance_id": "django__django-9999",
    "repo": "django/django",
    "base_commit": "deadbeef1234",
    "problem_statement": "Fix a bug in the ORM.",
    "hints_text": "Look at orm/query.py",
    "test_patch": "diff --git a/tests/test_orm.py b/tests/test_orm.py",
}


def _fake_cmd(mode: str = "success", patch: str | None = None) -> list[str]:
    """Return a reyn_cmd list that invokes fake_reyn_for_swe_bench with given mode."""
    env_overrides: list[str] = [
        f"FAKE_REYN_MODE={mode}",
    ]
    if patch is not None:
        env_overrides.append(f"FAKE_REYN_PATCH={patch}")

    # We use `env VAR=val python script.py` so the fake sees the env vars.
    # On all POSIX systems this is reliable; the test suite enforces this.
    return ["env", *env_overrides, sys.executable, str(_FIXTURE)]


# ── 1. parse_input: valid JSON ────────────────────────────────────────────────


def test_parse_input_valid() -> None:
    """Tier 2: valid SWE-bench JSON string → all fields accessible in returned dict."""
    from scripts.swe_bench_runner import parse_input

    result = parse_input(json.dumps(_VALID_INSTANCE))

    assert result["instance_id"] == "django__django-9999"
    assert result["repo"] == "django/django"
    assert result["base_commit"] == "deadbeef1234"
    assert result["problem_statement"] == "Fix a bug in the ORM."


# ── 2. parse_input: stdin path (same logic, string input) ─────────────────────


def test_parse_input_via_stdin_shim() -> None:
    """Tier 2: parse_input with extra optional fields present → succeeds."""
    from scripts.swe_bench_runner import parse_input

    instance = {**_VALID_INSTANCE, "extra_field": "ignored"}
    result = parse_input(json.dumps(instance))

    assert result["instance_id"] == "django__django-9999"
    assert "extra_field" in result  # extra fields pass through


# ── 3. parse_input: missing required field ────────────────────────────────────


def test_parse_input_missing_field() -> None:
    """Tier 2: JSON missing 'base_commit' → ValueError naming the missing field."""
    from scripts.swe_bench_runner import parse_input

    incomplete = {k: v for k, v in _VALID_INSTANCE.items() if k != "base_commit"}

    with pytest.raises(ValueError, match="base_commit"):
        parse_input(json.dumps(incomplete))


# ── 4. parse_input: malformed JSON ───────────────────────────────────────────


def test_parse_input_malformed_json() -> None:
    """Tier 2: non-JSON string → ValueError with 'malformed JSON' in message."""
    from scripts.swe_bench_runner import parse_input

    with pytest.raises(ValueError, match="malformed JSON"):
        parse_input("not json {{{")


# ── 5. format_output: success shape ──────────────────────────────────────────


def test_format_output_success_shape() -> None:
    """Tier 2: patch supplied → JSON with instance_id, model_name_or_path, model_patch."""
    from scripts.swe_bench_runner import format_output

    line = format_output("iid-1", "reyn", patch="diff --git a/f b/f\n")
    obj = json.loads(line)

    assert obj["instance_id"] == "iid-1"
    assert obj["model_name_or_path"] == "reyn"
    assert obj["model_patch"] == "diff --git a/f b/f\n"
    assert "error" not in obj


# ── 6. format_output: custom model name ──────────────────────────────────────


def test_format_output_custom_model() -> None:
    """Tier 2: --model-name custom → model_name_or_path reflects custom value."""
    from scripts.swe_bench_runner import format_output

    line = format_output("iid-2", "reyn-flash-v1", patch="diff\n")
    obj = json.loads(line)

    assert obj["model_name_or_path"] == "reyn-flash-v1"


# ── 7. format_output: error shape ────────────────────────────────────────────


def test_format_output_error_shape() -> None:
    """Tier 2: error supplied → JSON with instance_id, model_name_or_path, error."""
    from scripts.swe_bench_runner import format_output

    line = format_output("iid-3", "reyn", error="timeout after 600s")
    obj = json.loads(line)

    assert obj["instance_id"] == "iid-3"
    assert obj["model_name_or_path"] == "reyn"
    assert obj["error"] == "timeout after 600s"
    assert "model_patch" not in obj


# ── 8. extract_patch: marker-based ───────────────────────────────────────────


def test_extract_patch_from_marker() -> None:
    """Tier 2: reyn stdout with '=== Final Output ===' marker → patch extracted."""
    from scripts.swe_bench_runner import extract_patch

    stdout = (
        "skill           : swe_bench\n"
        "model           : standard\n"
        "\n"
        "=== Final Output ===\n"
        + json.dumps({
            "instance_id": "x",
            "patch": "diff --git a/a b/a\n--- a\n+++ b\n",
            "tests_passed": True,
            "attempts": 1,
        })
        + "\n"
    )

    patch = extract_patch(stdout)
    assert patch == "diff --git a/a b/a\n--- a\n+++ b\n"


# ── 9. extract_patch: nested data.patch ──────────────────────────────────────


def test_extract_patch_from_nested() -> None:
    """Tier 2: reyn output wraps result in {data: {patch: ...}} → correctly extracted."""
    from scripts.swe_bench_runner import extract_patch

    stdout = (
        "=== Final Output ===\n"
        + json.dumps({"type": "swe_bench_result", "data": {"patch": "nested_diff\n"}})
        + "\n"
    )

    patch = extract_patch(stdout)
    assert patch == "nested_diff\n"


def test_extract_patch_from_pretty_json_with_trailing_lines() -> None:
    """Tier 2: real `reyn run` stdout shape — indent=2 pretty JSON after the
    marker, followed by trailing token-usage / "events saved →" lines.

    Reproduces the gap where a plain json.loads of everything-after-the-marker
    fails on the trailing non-JSON lines (the multi-line patch never parsed).
    The fix (raw_decode) parses the first JSON value and ignores the trailing
    text.  Uses a multi-line patch so single-line scanning cannot accidentally
    succeed.
    """
    from scripts.swe_bench_runner import extract_patch

    patch_text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    stdout = (
        "skill           : swe_bench\n"
        "model           : standard\n"
        "\n"
        "=== Final Output ===\n"
        + json.dumps(
            {"type": "swe_bench_result", "data": {"patch": patch_text, "tests_passed": False}},
            indent=2,
        )
        + "\n"
        "Total tokens: 12345  cost: $0.0042\n"
        "\n"
        "events saved → /tmp/state/events/skill_runs/2026-06/x.jsonl\n"
    )

    assert extract_patch(stdout) == patch_text


# ── 10. run_reyn: success ────────────────────────────────────────────────────


def test_run_reyn_success() -> None:
    """Tier 2: fake reyn (success mode) → result has ok=True and a non-empty patch."""
    from scripts.swe_bench_runner import run_reyn

    result = run_reyn(_VALID_INSTANCE, reyn_cmd=_fake_cmd("success"), timeout=30)

    assert result["ok"] is True
    assert "patch" in result
    assert "diff" in result["patch"]


# ── 11. run_reyn: non-zero exit ───────────────────────────────────────────────


def test_run_reyn_nonzero() -> None:
    """Tier 2: fake reyn exits 1 → result has ok=False and error field."""
    from scripts.swe_bench_runner import run_reyn

    result = run_reyn(_VALID_INSTANCE, reyn_cmd=_fake_cmd("nonzero"), timeout=30)

    assert result["ok"] is False
    assert "error" in result
    assert "1" in result["error"]  # exit code in message


# ── 12. run_reyn: bad / unparseable output ────────────────────────────────────


def test_run_reyn_bad_output() -> None:
    """Tier 2: fake reyn exits 0 but stdout is not valid reyn output → ok=False."""
    from scripts.swe_bench_runner import run_reyn

    result = run_reyn(_VALID_INSTANCE, reyn_cmd=_fake_cmd("bad_output"), timeout=30)

    assert result["ok"] is False
    assert "error" in result


# ── 13. run_reyn: timeout ────────────────────────────────────────────────────


def test_run_reyn_timeout() -> None:
    """Tier 2: fake reyn sleeps forever → result has ok=False with 'timeout' in error."""
    from scripts.swe_bench_runner import run_reyn

    # Use a very short timeout (1 s) so the test finishes quickly.
    result = run_reyn(_VALID_INSTANCE, reyn_cmd=_fake_cmd("hang"), timeout=1)

    assert result["ok"] is False
    assert "timeout" in result["error"].lower()


# ── 14. main: --input file ────────────────────────────────────────────────────


def test_main_input_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Tier 2: main() with --input path → stdout is valid harness JSON."""
    from scripts.swe_bench_runner import main

    instance_file = tmp_path / "instance.json"
    instance_file.write_text(json.dumps(_VALID_INSTANCE), encoding="utf-8")

    exit_code = main([
        "--input", str(instance_file),
        "--model-name", "reyn-test",
        "--reyn-cmd", f"env FAKE_REYN_MODE=success {sys.executable} {_FIXTURE}",
        "--timeout", "30",
    ])

    assert exit_code == 0

    captured = capsys.readouterr()
    obj = json.loads(captured.out.strip())
    assert obj["instance_id"] == _VALID_INSTANCE["instance_id"]
    assert obj["model_name_or_path"] == "reyn-test"
    assert "model_patch" in obj


# ── 15. main: --stdin ─────────────────────────────────────────────────────────


def test_main_stdin_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: main() with --stdin reads JSON from sys.stdin → valid harness JSON."""
    import io

    from scripts.swe_bench_runner import main

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_VALID_INSTANCE)))

    exit_code = main([
        "--stdin",
        "--model-name", "reyn",
        "--reyn-cmd", f"env FAKE_REYN_MODE=success {sys.executable} {_FIXTURE}",
        "--timeout", "30",
    ])

    assert exit_code == 0

    captured = capsys.readouterr()
    obj = json.loads(captured.out.strip())
    assert obj["instance_id"] == _VALID_INSTANCE["instance_id"]
    assert "model_patch" in obj
