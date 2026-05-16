# Test Tier Auditor (`scripts/test_tier_audit.py`)

An AST-based linter that checks test files against the six rules in the
testing policy (`docs/deep-dives/contributing/testing.md`). Turns the policy
from a document people read once into a machine-checkable constraint applied
at every PR.

## Why

Before this tool, testing policy compliance was verified by reading test diffs
during code review. Reviewers could catch most violations, but the process was
manual, inconsistent across reviewers, and added latency to the PR loop. New
contributors who had not fully absorbed the policy could ship MagicMock-based
tests or missing Tier docstrings without either party noticing until review
comments arrived.

`test_tier_audit.py` makes the six most common violations detectable in
seconds, locally and in CI, before any reviewer looks at the code.

## Setup

No installation required — the script uses Python's `ast` module from the
standard library:

```bash
python scripts/test_tier_audit.py [files or dirs]
```

## Detection rules

The linter checks six rules. Each rule has a severity and a rationale grounded
in the testing policy.

### Rule 1 — Missing Tier docstring (ERROR)

Every test function must declare its Tier on the first line of its docstring:

```python
def test_something():
    """Tier 3a: router picks the correct skill for a planning message."""
    ...
```

The Tier label (`Tier 1`, `Tier 2`, `Tier 3a`, `Tier 3b`) must appear at the
start of the first docstring line. Functions without a docstring, or with a
docstring that does not start with a Tier label, trigger this error.

**Why:** Without the Tier label, there is no way to know whether a test
belongs at all (Tier 4 = do not write) or what contract it is asserting.

### Rule 2 — Format pinning (Tier 4 ERROR)

Expressions of the form `len(...) [<>=] N` pin the exact length of a string,
list, or output. Length pinning is a Tier 4 violation — it encodes
algorithm-level behavior that may change for valid reasons unrelated to the
contract being tested.

```python
# Violations
assert len(result) == 5
assert len(output.splitlines()) < 100

# Acceptable
assert len(result) > 0      # structural: non-empty check
```

**Why:** Format pinning produces brittle tests that fail on whitespace changes,
output reformatting, or valid algorithm improvements, without any contract
actually being violated.

### Rule 3 — Private state assertion (ERROR)

Assertions on private attributes (`obj._something`) reach into implementation
details that the class's public contract does not expose.

```python
# Violation
assert tracker._daily_tokens == 100
assert mgr._timers["c1"] is not None
```

**Why:** Private state is not part of the public API. Asserting on it means
the test breaks whenever the internal representation changes, regardless of
whether the behavior changed. Use the public surface or a `snapshot()`-style
read method instead.

### Rule 4 — MagicMock / AsyncMock / patch usage (ERROR)

`unittest.mock.MagicMock`, `AsyncMock`, and `patch` are prohibited. Use real
instances or the `LLMReplay` fake instead.

```python
# Violations
from unittest.mock import MagicMock, AsyncMock, patch

llm = MagicMock()
with patch("reyn.router.some_fn") as mock_fn:
    ...
```

**Why:** Mocks bypass real API contracts. A mock that accepts any call never
tells you whether the real collaborator would accept that call. Mocks silently
rot as the system evolves, because they pass even when the real interface has
changed incompatibly.

### Rule 5 — Bounded-life test in regular dir (WARNING)

Tests that contain phrases like `triggered_by`, `removed_by`, or
`scaffold_only` in their docstring or comments indicate they have a finite
expected lifetime and should live in `tests/scaffold/`, not in the regular
test directories.

**Why:** `tests/scaffold/` is the designated location for tests that exist to
catch a specific regression during a refactor and are deleted once the refactor
lands. Mixing them into the regular suite obscures the distinction between
permanent and transient tests.

### Rule 6 — Snapshot/golden test outside scaffold (ERROR)

Tests that write or read golden files (patterns: `golden`, `snapshot`,
`.gold`, `.expected`) outside `tests/scaffold/` violate the policy against
snapshot tests in the main suite.

**Why:** Snapshot tests in the main suite lock output format permanently,
creating maintenance burden and false failures on any output formatting change.
They belong in `tests/scaffold/` for the same reason as bounded-life tests.

## Flags

| Flag | Description |
|------|-------------|
| `files/dirs` | One or more file or directory paths to audit (positional) |
| `--strict` | Treat warnings as errors; exit 1 on any finding |
| `--check RULE` | Run only the named rule (repeatable; e.g. `--check rule1 --check rule4`) |
| `--quiet` | Suppress per-finding detail; print summary only |
| `--json` | Output findings as JSON (one object per finding, newline-delimited) |

## Output examples

### Default output

```
tests/test_router.py:42: [ERROR rule1] Missing Tier docstring: test_router_picks_skill
tests/test_router.py:87: [ERROR rule4] MagicMock usage: MagicMock
tests/test_util.py:12: [ERROR rule2] Format pinning: len(result) == 5

3 errors, 0 warnings
```

Exit code 1 when any errors are found; exit code 0 on a clean audit.

### `--quiet` output

```
3 errors, 0 warnings in 2 files
```

### `--json` output

```json
{"file": "tests/test_router.py", "line": 42, "severity": "ERROR", "rule": "rule1", "message": "Missing Tier docstring: test_router_picks_skill"}
{"file": "tests/test_router.py", "line": 87, "severity": "ERROR", "rule": "rule4", "message": "MagicMock usage: MagicMock"}
{"file": "tests/test_util.py", "line": 12, "severity": "ERROR", "rule": "rule2", "message": "Format pinning: len(result) == 5"}
```

## Integration with workflow

### As a pre-commit check

Run the auditor on changed test files before committing:

```bash
python scripts/test_tier_audit.py tests/
```

Or against only the files you are about to commit:

```bash
git diff --cached --name-only | grep '^tests/.*\.py$' | \
  xargs python scripts/test_tier_audit.py
```

### In PR review

When a PR adds new test files, run the auditor on them as part of review:

```bash
python scripts/test_tier_audit.py tests/test_new_feature.py
```

### Discovering existing violations in the test suite

Run against the entire suite with `--quiet` to get a count:

```bash
python scripts/test_tier_audit.py tests/ --quiet
```

Use `--check rule4` to focus on a single rule (e.g. finding all MagicMock
usage in the codebase):

```bash
python scripts/test_tier_audit.py tests/ --check rule4
```

### Using `--strict` for zero-tolerance CI

```bash
python scripts/test_tier_audit.py tests/ --strict
```

Exits 1 on any finding including warnings (Rule 5). Suitable for CI pipelines
where the entire suite must be clean.

## Limitations

The auditor is a heuristic indicator, not a formal verifier:

- **False positives exist and are acceptable.** The regex patterns for Rule 2
  and Rule 3 may flag valid code in unusual patterns (e.g. `len(enum_values)
  == 3` in a schema validation test that genuinely cares about the enum
  count). Inspect each finding before treating it as a violation.
- **AST analysis only.** The tool does not execute the test or resolve imports.
  It cannot detect mocks introduced via indirect imports or dynamic
  construction.
- **No cross-file analysis.** A test that delegates to a helper that uses
  MagicMock internally will not be flagged unless the helper file is also
  audited.

## See also

- [Replay testing reference](testing/replay.md) — `LLMReplay` fixture and
  how to write Tier 3 tests without mocks
