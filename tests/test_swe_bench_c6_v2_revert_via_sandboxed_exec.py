"""Tier 2: FP-0008 C6 v2 — revert test_patch targets via sandboxed_exec run_op.

Root cause recap: v1 (#1098, reverted in #1099) used ``import subprocess``
inside a ``mode: safe`` python step.  The safe-mode sandbox rejects subprocess
at AST parse time (``SafeModeViolation``) — the preprocessor aborted before any
code ran, and every verify instance errored.

v2 mechanism (#1115 Stage 2 migrated it off the deprecated ``shell`` op):
  1. ``parse_test_targets.py`` (mode: safe, pure re+json only) — parses
     ``+++ b/<path>`` headers from test_patch and returns a list of
     ``["git", "checkout", "HEAD", "--", <path>]`` argv lists.  No subprocess/os.
  2. ``iterate`` + ``run_op sandboxed_exec`` with ``args_from: {argv: _iter.item}`` —
     runs each checkout argv via op_runtime's sandboxed_exec handler, which
     anchors the subprocess to ``cwd=workspace.base_dir`` (FP-0008 PR-I, restored
     for sandboxed_exec) = the correct repo root, and routes through the run's
     EnvironmentBackend (host or container) instead of the host-only shell op.
  3. Mirror in ``report.md`` preprocessor for source-only final diff.

The verify / report phases declare a permissive ``default_sandbox_policy`` (D
mechanism): the OS applies it to every sandboxed_exec op in the phase, winning
over op fields. On host the platform default backend enforces it; on a container
backend it is ignored (the container is the boundary).

## Merge gate tests (mandatory per task spec)

(a) SafeModeViolation regression guard:
    parse_test_targets through real PythonRunner(mode="safe") must SUCCEED
    (no SafeModeViolation) — this directly reproduces the v1 failure condition.

(b) E2E real-preprocessor-path test:
    PreprocessorExecutor with real git repo → after preprocessor, contaminated
    test file is reverted AND ``git apply test_patch`` returns rc=0. A NoopBackend
    is injected so the test is deterministic across platforms and validates the
    migration as behavior-preserving (= old no-sandbox shell ≡ noop); seatbelt /
    landlock enforcement is exercised separately in test_op_sandboxed_exec.

All tests: real git (subprocess), real files in tmp_path, real PythonRunner /
op_runtime.  No MagicMock / AsyncMock / patch.  Docstrings open "Tier 2:".
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


# ── Git repo helpers ──────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in cwd; raise on non-zero unless check=False."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _setup_repo(tmp_path: Path) -> Path:
    """Create a git repo with a source file and a test file at HEAD.

    Returns the repo root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@test.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "mymod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (repo / "test_mymod.py").write_text(
        "def test_foo():\n    assert foo() == 1\n", encoding="utf-8"
    )
    _git(["add", "mymod.py", "test_mymod.py"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _make_test_patch() -> str:
    """A minimal unified diff targeting test_mymod.py."""
    return (
        "diff --git a/test_mymod.py b/test_mymod.py\n"
        "--- a/test_mymod.py\n"
        "+++ b/test_mymod.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def test_foo():\n"
        "     assert foo() == 1\n"
        "+    assert foo() != 2\n"
    )


# ── (a) SafeModeViolation regression guard ────────────────────────────────────


def test_parse_test_targets_safe_mode_no_violation() -> None:
    """Tier 2: parse_test_targets runs through real PythonRunner(mode='safe') without SafeModeViolation.

    This is the v1 regression guard.  v1 had ``import subprocess`` in the
    safe-mode module — PythonRunner raised PythonStepError(kind='SafeModeViolation')
    before any code ran.  v2 must succeed: the module uses only re + json.
    """
    from reyn.python_runner import PythonRunner, PythonStepError

    runner = PythonRunner()
    artifact = {
        "type": "apply_state",
        "data": {
            "test_patch": (
                "diff --git a/tests/test_x.py b/tests/test_x.py\n"
                "--- a/tests/test_x.py\n"
                "+++ b/tests/test_x.py\n"
                "@@ -1 +1 @@\n"
                "-old\n+new\n"
            ),
        },
    }
    # Must not raise — especially not PythonStepError(kind='SafeModeViolation').
    try:
        result = runner.run(
            skill_dir=_SKILL_ROOT,
            module="./parse_test_targets.py",
            function="parse_test_targets",
            mode="safe",
            artifact=artifact,
            timeout=30,
            allowed_modules=[],
        )
    except PythonStepError as exc:
        pytest.fail(
            f"PythonRunner(mode='safe') raised PythonStepError — this reproduces the v1 regression.\n"
            f"kind={exc.kind!r}, error={exc!s}\n"
            f"If kind='SafeModeViolation', the module uses a forbidden import "
            f"(subprocess/os/etc).  v2 must use only re+json."
        )
    # Result must be the expected list of checkout argv lists
    assert isinstance(result, list), f"Expected list, got {type(result).__name__}: {result!r}"
    assert any("tests/test_x.py" in argv for argv in result), (
        f"Expected an argv targeting tests/test_x.py. Got: {result}"
    )
    assert all(argv[:4] == ["git", "checkout", "HEAD", "--"] for argv in result), (
        f"All entries must be ['git','checkout','HEAD','--', <path>] argv lists. Got: {result}"
    )


def test_parse_test_targets_module_no_subprocess_import() -> None:
    """Tier 2: static guard — parse_test_targets.py has no top-level 'import subprocess' statement.

    Belt-and-suspenders alongside the PythonRunner test above.
    Checks AST-level imports, not docstring text, to avoid false positives.
    """
    import ast

    source = (_SKILL_ROOT / "parse_test_targets.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"subprocess", "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden and alias.name.split(".")[0] not in forbidden, (
                    f"parse_test_targets.py imports forbidden module {alias.name!r}. "
                    f"'subprocess'/'os' are not in PURE_STDLIB_ALLOWLIST — "
                    f"importing them in mode:safe triggers SafeModeViolation (= v1 bug)."
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module in forbidden or node.module.split(".")[0] in forbidden):
                pytest.fail(
                    f"parse_test_targets.py imports from forbidden module {node.module!r}. "
                    f"'subprocess'/'os' are not in PURE_STDLIB_ALLOWLIST."
                )


# ── Pure parsing unit tests (argv-list contract) ──────────────────────────────


def test_parse_test_targets_returns_checkout_argv() -> None:
    """Tier 2: parse_test_targets returns git checkout argv lists."""
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    patch = (
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
        "--- a/tests/test_foo.py\n"
        "+++ b/tests/test_foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    result = parse_test_targets({"data": {"test_patch": patch}})
    assert isinstance(result, list)
    assert result == [["git", "checkout", "HEAD", "--", "tests/test_foo.py"]], (
        f"Expected single checkout argv list. Got: {result}"
    )


def test_parse_test_targets_empty_patch_returns_empty() -> None:
    """Tier 2: empty test_patch → empty list."""
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    assert parse_test_targets({"data": {"test_patch": ""}}) == []
    assert parse_test_targets({"data": {}}) == []
    assert parse_test_targets({}) == []


def test_parse_test_targets_excludes_dev_null() -> None:
    """Tier 2: /dev/null on +++ line is excluded."""
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    # When /dev/null appears on a +++ line (deletion), it must be excluded.
    patch_with_dev_null_target = (
        "diff --git a/old.py b/new.py\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    )
    result = parse_test_targets({"data": {"test_patch": patch_with_dev_null_target}})
    assert all("/dev/null" not in argv for argv in result), (
        f"No argv should reference /dev/null: {result}"
    )


def test_parse_test_targets_deduplicates() -> None:
    """Tier 2: repeated +++ b/<path> lines produce a single argv."""
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    patch = "+++ b/tests/test_foo.py\n+++ b/tests/test_foo.py\n"
    result = parse_test_targets({"data": {"test_patch": patch}})
    assert result.count(["git", "checkout", "HEAD", "--", "tests/test_foo.py"]) == 1, (
        f"Deduplicated path must appear exactly once. Got: {result}"
    )


def test_parse_test_targets_multiple_files() -> None:
    """Tier 2: patch with two target files → two checkout argv lists."""
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    patch = (
        "+++ b/tests/test_foo.py\n"
        "+++ b/tests/test_bar.py\n"
    )
    result = parse_test_targets({"data": {"test_patch": patch}})
    assert ["git", "checkout", "HEAD", "--", "tests/test_foo.py"] in result
    assert ["git", "checkout", "HEAD", "--", "tests/test_bar.py"] in result


# ── (b) E2E real-preprocessor-path test ──────────────────────────────────────


def _preprocessor(skill, ws, events):
    """Build a PreprocessorExecutor with an injected NoopBackend.

    NoopBackend makes sandboxed_exec a plain subprocess (no enforcement),
    deterministic across platforms and behaviorally equivalent to the legacy
    no-sandbox shell op — which is exactly the behavior-preservation this
    migration claims.
    """
    from reyn.kernel.preprocessor_executor import PreprocessorExecutor
    from reyn.sandbox import NoopBackend

    return PreprocessorExecutor(
        skill=skill,
        workspace=ws,
        model="standard",
        events=events,
        subscribers=[],
        resolver=None,
        permission_resolver=None,
        sandbox_backend=NoopBackend(),
    )


def test_verify_preprocessor_reverts_contaminated_test_file_and_git_apply_succeeds(
    tmp_path: Path,
) -> None:
    """Tier 2: E2E — verify preprocessor reverts contaminated test file; git apply succeeds.

    This is the mandatory E2E merge gate test.  It runs the verify phase's
    full preprocessor chain through PreprocessorExecutor (real Workspace, real
    skill loaded from disk, real sandboxed_exec op_runtime execution via an
    injected NoopBackend in a real git repo).

    Setup:
      - git init + commit source file + test file
      - contaminate test_mymod.py (simulate apply-phase edit)
      - build artifact with test_patch targeting test_mymod.py

    Assert (after preprocessor):
      - test_mymod.py is reverted to HEAD content (contamination gone)
      - ``git apply test_patch`` returns rc=0 (loop-unblock proof)
    """
    from reyn.compiler.loader import load_dsl_skill
    from reyn.events.events import EventLog
    from reyn.workspace.workspace import Workspace

    # ── Setup: real git repo ──────────────────────────────────────────────────
    repo = _setup_repo(tmp_path)
    test_patch = _make_test_patch()

    # Contaminate test_mymod.py (simulates apply-phase LLM editing the test file)
    (repo / "test_mymod.py").write_text(
        "# apply-phase contamination\n"
        "def test_foo():\n"
        "    assert foo() == 99  # apply LLM wrote this\n",
        encoding="utf-8",
    )

    # Pre-condition: git apply FAILS on the contaminated working tree
    patch_file = repo / ".reyn_test.patch"
    patch_file.write_text(test_patch, encoding="utf-8")
    pre = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert pre.returncode != 0, (
        "Pre-condition: git apply must fail on contaminated test file before preprocessor. "
        f"stderr: {pre.stderr}"
    )

    # ── Load skill + run preprocessor ────────────────────────────────────────
    skill = load_dsl_skill(_SKILL_ROOT / "skill.md")
    verify_phase = skill.phases["verify"]

    events = EventLog()
    ws = Workspace(events=events, base_dir=repo)
    executor = _preprocessor(skill, ws, events)

    # Artifact: simulate apply_state with test_patch already set
    # (in production, sanitize_test_patch step sets data.test_patch;
    # we inject it directly here to avoid dependency on file.read step
    # which would need the workspace artifact path to exist)
    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test-instance",
            "files_edited": ["mymod.py"],
            "attempt": 1,
            "test_patch": test_patch,
        },
    }

    enriched, _usage = asyncio.run(
        executor.run(verify_phase, artifact, output_language=None)
    )

    # ── Assert: test file reverted ────────────────────────────────────────────
    reverted_content = (repo / "test_mymod.py").read_text(encoding="utf-8")
    assert "apply-phase contamination" not in reverted_content, (
        "test_mymod.py must be reverted to HEAD content after preprocessor. "
        f"Content: {reverted_content!r}"
    )
    assert "def test_foo" in reverted_content, (
        "test_mymod.py must contain the original HEAD content after revert. "
        f"Content: {reverted_content!r}"
    )

    # ── Assert: git apply test_patch returns rc=0 (THE loop-unblock proof) ───
    post = subprocess.run(
        ["git", "apply", str(patch_file)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert post.returncode == 0, (
        "After preprocessor revert, git apply <test_patch> must succeed (returncode 0). "
        "This proves the apply×verify test-collision loop is unblocked. "
        f"returncode={post.returncode}, stderr={post.stderr!r}, stdout={post.stdout!r}"
    )


def test_verify_preprocessor_clean_tree_no_error(tmp_path: Path) -> None:
    """Tier 2: E2E — verify preprocessor runs without error on clean working tree.

    When the working tree is already at HEAD (no contamination), the
    sandboxed_exec git checkout ops are no-ops and the preprocessor completes
    successfully.
    """
    from reyn.compiler.loader import load_dsl_skill
    from reyn.events.events import EventLog
    from reyn.workspace.workspace import Workspace

    repo = _setup_repo(tmp_path)
    test_patch = _make_test_patch()

    skill = load_dsl_skill(_SKILL_ROOT / "skill.md")
    verify_phase = skill.phases["verify"]

    events = EventLog()
    ws = Workspace(events=events, base_dir=repo)
    executor = _preprocessor(skill, ws, events)

    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test-instance",
            "files_edited": [],
            "attempt": 1,
            "test_patch": test_patch,
        },
    }

    # Must not raise even on a clean working tree
    enriched, _usage = asyncio.run(
        executor.run(verify_phase, artifact, output_language=None)
    )
    assert isinstance(enriched, dict), "Preprocessor must return a dict"


# ── Structure pins ────────────────────────────────────────────────────────────


def test_verify_md_has_parse_step_and_iterate_step() -> None:
    """Tier 2: verify.md preprocessor has parse_test_targets python step and iterate step."""
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    assert "parse_test_targets" in verify_md, (
        "verify.md must reference parse_test_targets in its preprocessor"
    )
    assert "type: iterate" in verify_md, (
        "verify.md must have an iterate step to run sandboxed_exec checkout argv"
    )
    assert "data._revert_cmds" in verify_md, (
        "verify.md must use data._revert_cmds as the iterate over path"
    )


def test_verify_md_iterate_uses_sandboxed_exec_argv_from_iter_item() -> None:
    """Tier 2: verify.md iterate run_op is sandboxed_exec binding argv from _iter.item.

    Replaces the old shell/cmd pin after the #1115 Stage 2 migration.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    assert "kind: sandboxed_exec" in verify_md, (
        "verify.md iterate run_op must use kind: sandboxed_exec (not shell)"
    )
    assert "kind: shell" not in verify_md, (
        "verify.md must no longer use the deprecated shell op"
    )
    assert "args_from" in verify_md and "_iter.item" in verify_md, (
        "verify.md must bind argv via args_from: {argv: _iter.item}"
    )
    assert "argv:" in verify_md, "verify.md iterate op must set argv"


def test_verify_md_parse_step_mode_safe() -> None:
    """Tier 2: verify.md parse_test_targets step is declared mode: safe."""
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    # Both sanitize_test_patch and parse_test_targets must declare mode: safe
    assert verify_md.count("mode: safe") >= 2, (
        "verify.md must declare mode: safe for both sanitize_test_patch and "
        f"parse_test_targets. Found {verify_md.count('mode: safe')} occurrences."
    )


def test_verify_and_report_declare_default_sandbox_policy() -> None:
    """Tier 2: verify.md and report.md declare a default_sandbox_policy (D mechanism)."""
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    report_md = (_SKILL_ROOT / "phases" / "report.md").read_text(encoding="utf-8")
    assert "default_sandbox_policy:" in verify_md, (
        "verify.md must declare default_sandbox_policy for its sandboxed_exec ops"
    )
    assert "default_sandbox_policy:" in report_md, (
        "report.md must declare default_sandbox_policy for its sandboxed_exec ops"
    )


def test_loader_wires_default_sandbox_policy_and_sandboxed_exec() -> None:
    """Tier 2: loading swe_bench produces Phase.default_sandbox_policy + sandboxed_exec allowed_ops.

    Behavioral pin for the parser→ir→expander wiring (the frontmatter key must
    reach the Phase object, not be dropped). Loads the real skill from disk.
    """
    from reyn.compiler.loader import load_dsl_skill
    from reyn.sandbox import SandboxPolicy

    skill = load_dsl_skill(_SKILL_ROOT / "skill.md")
    for phase_name in ("verify", "report"):
        phase = skill.phases[phase_name]
        assert "sandboxed_exec" in phase.allowed_ops, (
            f"{phase_name}.allowed_ops must include sandboxed_exec. Got: {phase.allowed_ops}"
        )
        policy = phase.default_sandbox_policy
        assert isinstance(policy, dict) and policy, (
            f"{phase_name}.default_sandbox_policy must be a non-empty dict reaching "
            f"the Phase object. Got: {policy!r}"
        )
        # SandboxPolicy must accept the declared kwargs (no unknown/typo'd key).
        SandboxPolicy(**policy)


def test_report_md_has_parse_step_and_iterate_step() -> None:
    """Tier 2: report.md preprocessor has parse_test_targets python step and iterate step."""
    report_md = (_SKILL_ROOT / "phases" / "report.md").read_text(encoding="utf-8")
    assert "parse_test_targets" in report_md, (
        "report.md must reference parse_test_targets in its preprocessor"
    )
    assert "type: iterate" in report_md, (
        "report.md must have an iterate step"
    )
    assert "preprocessor:" in report_md, (
        "report.md must have a preprocessor block"
    )
    assert "kind: sandboxed_exec" in report_md and "kind: shell" not in report_md, (
        "report.md iterate run_op must use sandboxed_exec, not the deprecated shell op"
    )


def test_report_md_uses_os_injected_skill_input_not_basedir_path() -> None:
    """Tier 2: report.md derives test_patch from the OS-injected _skill_input.

    #1115 Stage 0 removed report.md's ``run_op: file.read`` of the base_dir-
    coupled ``.reyn/artifacts/swe_bench/_input/...`` path. parse_test_targets
    now reads ``_skill_input.data.test_patch`` (Priority 0) — verified
    behaviorally here so the claim matches the test content.
    """
    from reyn.stdlib.skills.swe_bench.parse_test_targets import parse_test_targets

    report_md = (_SKILL_ROOT / "phases" / "report.md").read_text(encoding="utf-8")
    assert "swe_bench/_input/v01_swe_bench_input.json" not in report_md, (
        "report.md must NOT reference the base_dir-coupled _input magic path "
        "after #1115 Stage 0"
    )
    patch = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@\n-old\n+new\n"
    )
    artifact = {
        "type": "verify_state",
        "data": {"instance_id": "i"},
        "_skill_input": {
            "type": "swe_bench_input",
            "data": {"instance_id": "i", "test_patch": patch},
        },
    }
    assert parse_test_targets(artifact) == [["git", "checkout", "HEAD", "--", "tests/test_x.py"]]


def test_apply_md_has_source_only_rule() -> None:
    """Tier 2: apply.md has the SOURCE files only domain rule."""
    apply_md = (_SKILL_ROOT / "phases" / "apply.md").read_text(encoding="utf-8")
    assert "SOURCE files only" in apply_md or "source files only" in apply_md.lower(), (
        "apply.md must contain the 'SOURCE files only' domain rule"
    )
    assert "reverted" in apply_md or "harness owns" in apply_md, (
        "apply.md must mention test edits are reverted or harness owns test files"
    )


def test_plan_md_has_source_only_rule() -> None:
    """Tier 2: plan.md has the SOURCE files only domain rule."""
    plan_md = (_SKILL_ROOT / "phases" / "plan.md").read_text(encoding="utf-8")
    assert "SOURCE files only" in plan_md or "source files only" in plan_md.lower(), (
        "plan.md must contain the 'SOURCE files only' domain rule"
    )


def test_skill_md_registers_parse_test_targets() -> None:
    """Tier 2: skill.md registers parse_test_targets as a safe-mode python step."""
    skill_md = (_SKILL_ROOT / "skill.md").read_text(encoding="utf-8")
    assert "parse_test_targets.py" in skill_md, (
        "skill.md must list parse_test_targets.py in permissions.python"
    )
    assert "parse_test_targets" in skill_md, (
        "skill.md must reference the parse_test_targets function name"
    )
