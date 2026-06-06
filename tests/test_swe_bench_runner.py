"""Tier 2: OS invariant — swe_bench_runner.py I/O contract.

Pins the wrapper's pure I/O logic (parse / format) + the main() contract:

1.  test_parse_input_valid              — valid JSON dict → parsed correctly
2.  test_parse_input_via_stdin_shim     — same logic exercised via string (stdin path)
3.  test_parse_input_missing_field      — missing required field → ValueError
4.  test_parse_input_malformed_json     — malformed JSON → ValueError
5.  test_format_output_success_shape   — patch supplied → correct harness JSON shape
6.  test_format_output_custom_model    — --model-name propagates to output
7.  test_format_output_error_shape     — error supplied → error JSON shape
8.  test_main_input_requires_docker    — host skill path retired → docker required
9.  test_main_stdin_reads_input        — --stdin parse path intact

#187 retire: the swe_bench skill + its host/container subprocess solver
(``run_reyn`` / ``run_reyn_in_container`` / ``extract_patch``) were removed; the
runner now solves only via the general agent (``reyn run-once``) in a container,
so the former fake-reyn-subprocess tests (extract_patch / run_reyn) are gone.

No unittest.mock / AsyncMock / MagicMock / patch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Minimal valid SWE-bench instance
_VALID_INSTANCE = {
    "instance_id": "django__django-9999",
    "repo": "django/django",
    "base_commit": "deadbeef1234",
    "problem_statement": "Fix a bug in the ORM.",
    "hints_text": "Look at orm/query.py",
    "test_patch": "diff --git a/tests/test_orm.py b/tests/test_orm.py",
}


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


# ── 14. main: faithful eval requires docker (host skill path retired) ──────────


def test_main_input_requires_docker(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Tier 2: the swe_bench skill (and its host solver path) was retired — main()
    now solves only via the general agent (`reyn run-once`) in a per-instance
    container, so a non-docker invocation is an honest error (no silent host run)."""
    from scripts.swe_bench_runner import main

    instance_file = tmp_path / "instance.json"
    instance_file.write_text(json.dumps(_VALID_INSTANCE), encoding="utf-8")

    exit_code = main(["--input", str(instance_file), "--model-name", "reyn-test"])

    assert exit_code == 1
    assert "requires --env-backend=docker" in capsys.readouterr().err


# ── 15. main: --stdin still reads input (parse path intact) ────────────────────


def test_main_stdin_reads_input(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: --stdin reads + parses the instance (the input path is intact); it
    then hits the docker-required gate (run-once-only contract)."""
    import io

    from scripts.swe_bench_runner import main

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_VALID_INSTANCE)))
    exit_code = main(["--stdin", "--model-name", "reyn"])
    # parse succeeded (no 'invalid input' error); the docker-required gate fired
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "invalid input" not in err
    assert "requires --env-backend=docker" in err
