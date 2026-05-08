# B12-R1 — B11-NEW-1 Diagnosis (copy_to_work preprocessor run_op permission_denied)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD at diagnosis | `c7c09fa` |
| Bug ID | B11-NEW-1 (first seen as B8-NEW-1) |
| Verdict | **root cause confirmed — structurally fixable** |

## Symptom

In every `partial` integration run (B11 Step 2, runs 1 and 4):

```
Phase 'copy_to_work' preprocessor step[1] run_op (file): read from
'<main_repo>/src/reyn/stdlib/skills/direct_llm/skill.md' was not approved.
```

step[0] (`python compute_paths`) succeeds; step[1] (`run_op file glob`) fails.

## Reproduction (deterministic, no LLM needed)

```python
from pathlib import Path
from reyn.permissions.permissions import PermissionResolver, _in_default_read_zone
from reyn.skill.skill_paths import stdlib_root

# Simulate worktree CWD
import os; os.chdir("/tmp/some_worktree")

target = str(stdlib_root() / "skills" / "direct_llm" / "skill.md")
# Before fix: returns False — permission gate denies the read
print(_in_default_read_zone(target))  # False  ← pre-fix
```

Verified at HEAD `c7c09fa` (pre-fix): `_in_default_read_zone` returns `False`
for stdlib paths when CWD is a worktree directory.

## Root cause (confirmed)

**Editable install + git worktree CWD mismatch.**

### Chain of events

1. `reyn chat` runs from a git worktree: CWD = `.../sandbox_2/.claude/worktrees/<id>/`
2. `startup_guard` processes `skill_improver`'s declared path `"src/reyn/stdlib/skills"`.
   It resolves the relative path as `<worktree>/src/reyn/stdlib/skills` —
   which is a symlink back to the main repo.  Resolution: **within CWD** → skips
   `session_approve_path` (no entry recorded; it's the "default zone").
3. `copy_to_work` preprocessor step[0] (`python compute_paths`) calls
   `resolve_skill_path("direct_llm")` → `stdlib_root() / "skills" / "direct_llm"`.
4. `stdlib_root()` uses `Path(__file__).parent.parent / "stdlib"` — always the
   **installed package** path: `.../sandbox_2/src/reyn/stdlib`.
5. step[1] (`run_op file glob`) calls `require_file_read` with the absolute path
   `.../sandbox_2/src/reyn/stdlib/skills/direct_llm/skill.md`.
6. `_in_default_read_zone(path)` checks `path.relative_to(Path.cwd())`:
   - `Path.cwd()` = `.../sandbox_2/.clone/worktrees/<id>/` (the worktree)
   - `path` = `.../sandbox_2/src/reyn/stdlib/...` (the main repo)
   - `relative_to` raises `ValueError` → returns `False`
7. No `session_approve_path` entry exists for this absolute path → **PermissionError**.

### Why startup_guard didn't help

`startup_guard` resolved `"src/reyn/stdlib/skills"` relative to the worktree
CWD and found it in-zone (worktrees are typically symlinked so the path exists).
It therefore short-circuits and never calls `session_approve_path`.  Even if it
did, the worktree-relative resolution would not match the installed-package
absolute path that `stdlib_root()` returns.

### Why it was intermittently "fixed" in batch 10 Run 2

When `reyn chat` was invoked from the main repo dir (not a worktree), CWD =
`.../sandbox_2/` and `.../sandbox_2/src/reyn/stdlib/...` was within CWD →
`_in_default_read_zone` returned `True` → no error.  This is why batch 10
appeared to work: N=1 lucky invocation from main repo.

## Hypotheses evaluated

| Hypothesis | Verdict |
|---|---|
| A: `startup_guard` skips non-interactive mode (G15 gap) | **Not applicable** — G15 already fixed in commit `651a053` |
| B: run_op uses a different permission path than direct `file.read` | **Partially correct** — the real issue is that the path itself differs (worktree vs installed package root) |
| C: skill_improver declared paths don't cover stdlib absolute paths | **Ruled out** — declarations cover `src/reyn/stdlib/skills`; the issue is path resolution |
| D: _in_default_read_zone has a CWD snapshot problem | **Confirmed root cause** — uses `Path.cwd()` at call time; worktree CWD != stdlib root |

## Fix approach chosen

Extend `_in_default_read_zone` to include the installed `stdlib_root()` as a
second default zone (alongside CWD).  The stdlib is the OS's own bundled files —
not user-controlled content — so granting universal read access does not violate
P7 or the care boundary.

Alternative rejected: blanket auto-approve for all `run_op` file reads
(P3/care boundary violation — OS would silently approve skill-specific ops).
