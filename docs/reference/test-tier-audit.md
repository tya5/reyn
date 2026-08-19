# Test Tier Auditor (`scripts/test_tier_audit.py`)

An AST-based linter that checks test files against eight rules grounded in the
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

`test_tier_audit.py` makes the eight most common violations detectable in
seconds, locally and in CI, before any reviewer looks at the code.

## Setup

No installation required — the script uses Python's `ast` module from the
standard library:

```bash
python scripts/test_tier_audit.py [files or dirs]
```

## Detection rules

The linter checks eight rules. Each rule has a severity and a rationale
grounded in the testing policy.

### Rule 1 — Missing Tier docstring (ERROR)

Every test function must declare its Tier on the first line of its docstring:

```python
def test_something():
    """Tier 3a: router picks the correct skill for a planning message."""
    ...
```

The Tier label (`Tier 1`, `Tier 2`, `Tier 3a`, `Tier 3b`) must appear at the
start of the first docstring line, followed immediately by a colon
(`^Tier [123][abc]?:`). Functions without a docstring, or with a
docstring that does not match this pattern, trigger this error.

**Common footgun:** a qualifier between the tier and the colon fails the audit:

```python
# FAIL — parenthetical before the colon
"""Tier 2 (MUST-1): checks the invariant."""

# PASS — qualifier after the colon
"""Tier 2: (MUST-1) checks the invariant."""
```

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

### Rule 7 — Fake attribute on a real object (ERROR)

An `obj.attr = value` assignment where `attr` is not an attribute the
production class declares, suppressed with `# type: ignore[attr-defined]`
(#4873, #3037's own named pattern — a real instance mock-ified in place
instead of using a Fake). mypy already flags this on its own — the
`[attr-defined]` code on an *assignment* means exactly "the class has no
such attribute" — so this rule needs no detector of its own, only a ban
on the suppression comment that silences it.

Scoped to assignment lines only: reading an already-flagged private
attribute elsewhere (e.g. `host.events.emitted`) is a narrower, different
complaint (a type mypy can't see, not an attribute that doesn't exist)
and stays out of scope. Whole-file, not per-`test_*`-function like Rules
1–6: the real violations that motivated this rule mostly lived inside
module-level helper functions (e.g. `_noop_handler`, a fixture a test
calls) that a per-test-function walk never reaches.

**Known gap** (disclosed, not claimed away): a dynamic `setattr(ns, k,
v)` cannot be seen by any syntax gate — this rule only catches the
literal `obj.attr = value` shape.

**Why:** A `# type: ignore[attr-defined]` next to an attribute assignment
is the author's own trace of having silenced a real defect deliberately —
the production object doesn't have this attribute, and mypy already knew
it. Use the public API, extend the class, or thread the value through
explicitly instead of attaching it post-hoc.

### Rule 8 — Private read with a same-class public alternative (ERROR)

An `obj._x` **read** (#4864 — not only inside an `assert`, unlike Rule 3:
routing a private read through a local variable one line before the
assert made 126 real sites invisible to an assert-scoped walk) where
`obj` is type-evident as a class that ALSO publishes `x` via a same-class
`@property`. The index is built once per run, class-name-keyed (not a
global name match) — the same private attribute name assigned in several
unrelated classes only trips this rule on the ONE class that also backs
it with a `@property`.

`obj` must be a bare name the AST can type — a parameter annotation, a
local factory/fixture call, or a direct constructor call; `self._x`
inside the class's own methods is exempt (that access is legitimate), and
a private WRITE (`obj._x = value`) is Rule 7's territory, not this one.

**Known gaps, disclosed, not claimed away**: `obj` is restricted to a
bare name, not an attribute chain (`a.b._x`) — a deliberately narrower
net than Rule 3's, since this rule's precondition is type evidence on the
base. Two more false-negative shapes (tuple-unpack destructuring, chained
attribute access) were found after landing and are tracked separately
(#4906) rather than silently left undocumented. The 109 real sites found
and fixed at landing (across 41 files) were a measured **floor at that
moment**, not a claim of total coverage — a clean run today does not mean
no more of this shape exists, only that the two disclosed gaps (and any
undiscovered ones) aren't visible to it yet.

**Why:** The finding is a repair obligation, not a bare detector — the
suggested fix is "use the public property," but the message also warns
against manufacturing a same-named `@property` just to silence the gate
without checking whether it actually answers what the test needs (#4866:
exactly this shape once "ratified the encapsulation break instead of
closing it").

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

Exit code 1 **also** when the targets resolve to zero test files (#4577) — a
mistyped path, a file another PR has since moved, or an empty shell variable.
That case is neither "errors found" nor "a clean audit": nothing was audited,
and the run says so rather than reporting the colour of a pass. The resolved
targets are echoed alongside the message, since a typo is easier to see next to
the path it was meant to be.

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
