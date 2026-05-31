"""Tier 2: FP-0008 PR-N15 — test_patch deterministic workspace passthrough.

Root cause: `apply.md` instructed the apply-phase LLM to echo `test_patch`
verbatim from the plan input into the `apply_state` output artifact.  Weak
models (gemini-flash-lite) dropped or nulled the field, causing schema
validation failure at the verify-phase entry.

Fix shape (PR-N15, as evolved by #1115 Stage 0):
  - `test_patch` removed from `apply_state` schema (no more LLM echo).
  - `apply.md` "Carry test_patch" instruction removed.
  - The OS injects the skill's original entry artifact at the reserved
    top-level ``_skill_input`` binding before each preprocessor runs.
    ``verify.md`` / ``report.md`` read ``_skill_input.data.test_patch`` from
    there — no workspace ``file.read`` of a base_dir-coupled magic path.
    (#1115 Stage 0 removed the prior
    ``.reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json`` file.read,
    which coupled the read to base_dir and breaks once the repo FS routes
    through a backend.)
  - `sanitize_test_patch.py` reads from ``_skill_input.data.test_patch``
    (Priority 0), falling back to the legacy ``data._input_raw.content``
    (Priority 1, retained for unit tests), then ``data.test_patch`` (Priority
    2) or top-level flat dict (Priority 3, unit-test compat).

This file pins:
  (a) `apply_state` schema does NOT include `test_patch` as a required field.
  (b) `apply.md` does NOT contain the "Carry test_patch" instruction.
  (c) `verify.md` / `report.md` no longer reference the base_dir-coupled
      magic path, and `verify.md`'s first preprocessor step is the python
      sanitizer; `sanitize_test_patch` reads test_patch from the OS-injected
      ``_skill_input`` binding (Priority 0 = the #1115 Stage 0 mechanism).
  (d) `sanitize_test_patch` extracts test_patch from the legacy workspace
      ``_input_raw.content`` payload (Priority 1, back-compat).
  (e) `sanitize_test_patch` handles the full runtime artifact shape
      ``{"type": "apply_state", "data": {"test_patch": "..."}}`` correctly
      (Priority 2 path).
  (f) `sanitize_test_patch` returns empty string when all sources are absent.
  (g) Regression guard: LLM-null test_patch in artifact + entry input present
      → entry-input value wins (= the 13977 failure mode stays fixed).

Tier rule discipline: every test docstring opens with Tier 2; no mocks; no
private-state assertions; no format-pinning.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)

# ── (a) apply_state schema: test_patch not in required ───────────────────────


def test_apply_state_schema_no_test_patch_required() -> None:
    """Tier 2: apply_state.yaml must not require test_patch.

    PR-N15 removes test_patch from apply_state so the apply-phase LLM
    is never asked to echo the large diff string.  A weak model that
    cannot faithfully copy test_patch will no longer cause a schema
    validation failure.
    """
    raw = (_SKILL_ROOT / "artifacts" / "apply_state.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    required = doc.get("schema", {}).get("required", [])
    assert "test_patch" not in required, (
        "apply_state.schema.required must not include 'test_patch' after PR-N15. "
        f"Actual required: {required}"
    )


def test_apply_state_schema_no_test_patch_property() -> None:
    """Tier 2: apply_state.yaml must not define a test_patch property.

    Removes the field entirely so the LLM is not tempted to fill it
    (a defined-but-not-required field still leaks through the LLM prompt).
    """
    raw = (_SKILL_ROOT / "artifacts" / "apply_state.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    properties = doc.get("schema", {}).get("properties", {})
    assert "test_patch" not in properties, (
        "apply_state.schema.properties must not include 'test_patch' after PR-N15. "
        f"Actual properties: {sorted(properties.keys())}"
    )


# ── (b) apply.md: no "Carry test_patch" instruction ──────────────────────────


def test_apply_md_no_carry_test_patch_instruction() -> None:
    """Tier 2: apply.md must not contain the 'Carry test_patch' instruction.

    PR-N15 removes the line that asked the LLM to echo test_patch so
    the apply phase has no knowledge of test_patch propagation.
    """
    apply_md = (_SKILL_ROOT / "phases" / "apply.md").read_text(encoding="utf-8")
    # The old instruction contained the words "Carry" and "test_patch"
    # together.  Both must no longer co-occur.
    assert "Carry" not in apply_md or "test_patch" not in apply_md or (
        "Carry" not in apply_md and "test_patch" not in apply_md
    ), (
        "apply.md must not contain 'Carry ... test_patch' instruction after PR-N15"
    )
    # Stronger check: the specific instruction phrase must be absent
    assert "Carry `test_patch`" not in apply_md, (
        "apply.md still contains the old 'Carry `test_patch`' instruction — "
        "this must be removed in PR-N15"
    )


# ── (c) #1115 Stage 0: OS-injected _skill_input replaces base_dir file.read ──


def test_verify_and_report_md_drop_basedir_coupled_magic_path() -> None:
    """Tier 2: verify.md / report.md no longer reference the base_dir magic path.

    #1115 Stage 0 removed the ``run_op: file.read`` of
    ``.reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json``.  That path
    coupled the deterministic test_patch read to ``base_dir``, which breaks
    once the repo filesystem routes through a backend.  The OS-injected
    ``_skill_input`` binding is base_dir-independent.  This guards against the
    coupling being reintroduced.
    """
    for phase in ("verify", "report"):
        md = (_SKILL_ROOT / "phases" / f"{phase}.md").read_text(encoding="utf-8")
        assert "swe_bench/_input/v01_swe_bench_input.json" not in md, (
            f"{phase}.md must NOT reference the base_dir-coupled _input magic "
            "path after #1115 Stage 0 — the OS injects _skill_input instead"
        )
        assert "into: data._input_raw" not in md, (
            f"{phase}.md must NOT contain the file.read 'into: data._input_raw' "
            "step after #1115 Stage 0"
        )


def test_verify_md_first_preprocessor_step_is_python_sanitizer() -> None:
    """Tier 2: verify.md's first preprocessor step is the python sanitizer.

    With the file.read run_op removed (#1115 Stage 0), the sanitizer no longer
    depends on a preceding read step — it consumes the OS-injected
    ``_skill_input`` directly.  So the python step is now first; any ``run_op``
    that remains (the iterate's inner shell op) must come AFTER it.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    python_pos = verify_md.find("type: python")
    run_op_pos = verify_md.find("type: run_op")
    assert python_pos != -1, "verify.md must contain a 'type: python' step"
    assert run_op_pos == -1 or python_pos < run_op_pos, (
        "The python sanitizer must precede any run_op step in verify.md after "
        "#1115 Stage 0 (no file.read run_op precedes it anymore)"
    )


def _make_skill_input_artifact(test_patch, *, inner_test_patch=...) -> dict:
    """Build the working artifact as the preprocessor sees it under #1115 Stage 0.

    The OS injects the entry ``swe_bench_input`` artifact at the top-level
    ``_skill_input`` binding (sibling of ``data``).  ``inner_test_patch`` (when
    given) sets ``data.test_patch`` to simulate a weak-model apply output;
    default (``...``) leaves it absent.
    """
    inner_data: dict = {
        "instance_id": "test__test-1",
        "files_edited": ["foo.py"],
        "attempt": 1,
    }
    if inner_test_patch is not ...:
        inner_data["test_patch"] = inner_test_patch
    return {
        "type": "apply_state",
        "data": inner_data,
        "_skill_input": {
            "type": "swe_bench_input",
            "data": {
                "instance_id": "test__test-1",
                "repo": "test/test",
                "base_commit": "abc123",
                "problem_statement": "Fix a bug",
                "test_patch": test_patch,
            },
        },
    }


def test_sanitizer_reads_from_skill_input_binding() -> None:
    """Tier 2: sanitizer extracts test_patch from the OS-injected _skill_input.

    This is the #1115 Stage 0 primary path (Priority 0).  The OS injects the
    entry artifact at ``_skill_input``; the sanitizer reads
    ``_skill_input.data.test_patch`` — not from the apply LLM output.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    patch_str = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    artifact = _make_skill_input_artifact(patch_str)
    assert sanitize_test_patch(artifact) == patch_str


def test_sanitizer_skill_input_normalizes_crlf() -> None:
    """Tier 2: sanitizer normalizes CRLF when reading from _skill_input."""
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    raw = "diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@\r\n-a\r\n+b\r\n"
    result = sanitize_test_patch(_make_skill_input_artifact(raw))
    assert "\r" not in result


def test_sanitizer_skill_input_wins_over_null_inner_test_patch() -> None:
    """Tier 2: regression guard — _skill_input wins over null data.test_patch.

    Simulates a weak-model apply output where ``data.test_patch`` is null.
    The OS-injected ``_skill_input`` must take priority (= the 13977 failure
    mode stays fixed under the #1115 Stage 0 mechanism).
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    real_patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    artifact = _make_skill_input_artifact(real_patch, inner_test_patch=None)
    assert sanitize_test_patch(artifact) == real_patch


def test_parse_test_targets_reads_from_skill_input_binding() -> None:
    """Tier 2: parse_test_targets derives revert commands from _skill_input.

    report.md has no sanitize step, so it relies on parse_test_targets reading
    test_patch directly from the OS-injected ``_skill_input`` (Priority 0).
    """
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    patch = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@\n-old\n+new\n"
    )
    artifact = _make_skill_input_artifact(patch)
    assert parse_test_targets(artifact) == [["git", "checkout", "HEAD", "--", "tests/test_x.py"]]


# ── (d) sanitizer: Priority 1 — workspace _input_raw path ───────────────────


def _make_workspace_artifact(test_patch: str) -> dict:
    """Build the full artifact dict as the preprocessor sees it after run_op.

    The run_op step injects the workspace file read result at data._input_raw.
    The read result shape is {"status": "ok", "content": "<JSON>", ...}.
    The JSON is the full swe_bench_input artifact.
    """
    swe_bench_input_artifact = {
        "type": "swe_bench_input",
        "data": {
            "instance_id": "test__test-1",
            "repo": "test/test",
            "base_commit": "abc123",
            "problem_statement": "Fix a bug",
            "test_patch": test_patch,
        },
    }
    return {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
            "_input_raw": {
                "status": "ok",
                "content": json.dumps(swe_bench_input_artifact),
            },
        },
    }


def test_sanitizer_reads_from_workspace_input_raw() -> None:
    """Tier 2: sanitizer extracts test_patch from data._input_raw.content.

    This is the PR-N15 primary fix path (Priority 1).  When the run_op
    step has injected _input_raw.content with the workspace JSON, the
    sanitizer reads test_patch from there — not from the apply LLM output.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    patch_str = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    artifact = _make_workspace_artifact(patch_str)
    result = sanitize_test_patch(artifact)
    assert result == patch_str, (
        f"Sanitizer must read test_patch from data._input_raw.content. "
        f"Expected: {patch_str!r}, got: {result!r}"
    )


def test_sanitizer_workspace_path_normalizes_crlf() -> None:
    """Tier 2: sanitizer normalizes CRLF in test_patch from workspace source."""
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    raw = "diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@\r\n-a\r\n+b\r\n"
    artifact = _make_workspace_artifact(raw)
    result = sanitize_test_patch(artifact)
    assert "\r" not in result, (
        "CRLF normalization must apply even when reading from workspace source"
    )


# ── (e) sanitizer: Priority 2 — full runtime artifact shape ─────────────────


def test_sanitizer_reads_from_full_artifact_data_dict() -> None:
    """Tier 2: sanitizer reads test_patch from artifact['data']['test_patch'].

    Priority 2 path: when _input_raw is absent but test_patch is present
    in the artifact's inner data dict (full runtime artifact shape).  This
    was the broken path before PR-N15 — the old code did data.get('test_patch')
    where data was the FULL artifact dict, looking at the wrong level.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    patch_str = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    # Full artifact shape without _input_raw — must still work via fallback
    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
            "test_patch": patch_str,
        },
    }
    result = sanitize_test_patch(artifact)
    assert result == patch_str, (
        f"Sanitizer must read test_patch from artifact['data']['test_patch']. "
        f"Expected: {patch_str!r}, got: {result!r}"
    )


# ── (f) sanitizer: empty when all sources absent ─────────────────────────────


def test_sanitizer_empty_when_no_sources() -> None:
    """Tier 2: sanitizer returns empty string when all test_patch sources absent.

    Both _input_raw and test_patch missing → empty string (verify phase
    instruction handles this case explicitly).
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
        },
    }
    assert sanitize_test_patch(artifact) == ""


def test_sanitizer_empty_when_workspace_content_invalid_json() -> None:
    """Tier 2: sanitizer returns empty string when _input_raw.content is not JSON.

    on_error: empty in the run_op step can produce _input_raw=None or
    an empty content field.  The sanitizer must not raise in these cases.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": [],
            "attempt": 1,
            "_input_raw": {"status": "error", "content": "NOT JSON {{{"},
        },
    }
    # Should not raise; falls through to missing test_patch → empty string
    result = sanitize_test_patch(artifact)
    assert result == ""


# ── (g) regression: LLM-null + workspace present → workspace wins ────────────


def test_sanitizer_workspace_wins_over_null_artifact_test_patch() -> None:
    """Tier 2: regression guard for 13977 failure mode.

    The original bug: apply LLM emitted test_patch=null → schema fail.
    After PR-N15, apply_state no longer has test_patch at all, and the
    verify preprocessor injects test_patch from the workspace.

    This test simulates the scenario where _input_raw is present (from
    workspace passthrough) and test_patch in the inner data is absent/null
    (= what a weak-model apply-phase output would look like if test_patch
    were still in the schema).  The workspace value must win.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    real_patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
            # test_patch in the artifact data is null (= weak LLM dropped it)
            "test_patch": None,
            # But _input_raw has the real value from the workspace
            "_input_raw": {
                "status": "ok",
                "content": json.dumps({
                    "type": "swe_bench_input",
                    "data": {
                        "instance_id": "test__test-1",
                        "test_patch": real_patch,
                    },
                }),
            },
        },
    }
    result = sanitize_test_patch(artifact)
    assert result == real_patch, (
        "Workspace _input_raw must win over null test_patch in artifact. "
        f"Expected workspace value: {real_patch!r}, got: {result!r}"
    )


# ── (h) #1115 Stage 0: OS-inject round-trip through the real preprocessor ────


def test_preprocessor_injects_skill_input_and_strips_it(tmp_path: Path) -> None:
    """Tier 2: E2E — OS-injected _skill_input drives the deterministic read.

    Runs the real verify-phase preprocessor (real Workspace + real skill loaded
    from disk + real safe-mode python subprocess) with the entry test_patch
    supplied ONLY via ``skill_input`` (= what the OS holds at
    ``run_state.skill_input``), NOT in the phase input artifact's data.

    Asserts the full #1115 Stage 0 contract:
      - ``data.test_patch`` is populated from ``_skill_input`` (the OS injected
        it at the top-level binding → sanitize_test_patch read it).
      - ``_skill_input`` is stripped from the enriched artifact (no leak into
        the LLM-facing frame / stored artifact).
    """
    from reyn.compiler.loader import load_dsl_skill
    from reyn.events.events import EventLog
    from reyn.kernel.preprocessor_executor import PreprocessorExecutor
    from reyn.workspace.workspace import Workspace

    skill = load_dsl_skill(_SKILL_ROOT / "skill.md")
    verify_phase = skill.phases["verify"]
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    executor = PreprocessorExecutor(
        skill=skill,
        workspace=ws,
        model="standard",
        events=events,
        subscribers=[],
        resolver=None,
        permission_resolver=None,
    )

    patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    # Phase input carries NO test_patch; the entry input does (OS-held).
    artifact = {
        "type": "apply_state",
        "data": {"instance_id": "i", "files_edited": ["x"], "attempt": 1},
    }
    skill_input = {
        "type": "swe_bench_input",
        "data": {"instance_id": "i", "test_patch": patch},
    }

    enriched, _usage = asyncio.run(
        executor.run(
            verify_phase, artifact, output_language=None, skill_input=skill_input,
        )
    )

    assert enriched["data"].get("test_patch") == patch, (
        "data.test_patch must be derived from the OS-injected _skill_input "
        f"(got {enriched['data'].get('test_patch')!r})"
    )
    assert "_skill_input" not in enriched, (
        "_skill_input must be stripped from the enriched artifact so it does "
        "not leak into the LLM frame or the stored artifact"
    )
