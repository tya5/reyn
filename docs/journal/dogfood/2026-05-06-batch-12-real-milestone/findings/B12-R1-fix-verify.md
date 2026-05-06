# B12-R1 — B11-NEW-1 Fix Verification

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| Fix commit | (this commit — see git log) |
| Pre-fix HEAD | `c7c09fa` |
| Fix verdict | ✅ **verified** — deterministic structural fix |
| Test count | +6 Tier 2 (new) + 6 updated |

## Fix summary

**File**: `src/reyn/permissions/permissions.py`

Extended `_in_default_read_zone` to check `stdlib_root()` as a second default
read zone (in addition to CWD).  Used lazy initialization (`_STDLIB_READ_ZONE_RESOLVED`
flag) to avoid a circular import at module load time.

Two zones now always granted without explicit approval:
1. CWD (existing behavior, unchanged)
2. `stdlib_root()` — the installed reyn OS package files (new, B11-NEW-1 fix)

## Pre-fix vs post-fix behavior

| Scenario | Pre-fix | Post-fix |
|---|---|---|
| CWD = main repo, read stdlib path | Allowed (CWD covers it) | Allowed (CWD covers it) |
| CWD = worktree, read stdlib path | **Denied** (B11-NEW-1) | **Allowed** (stdlib zone) |
| CWD = anywhere, read external path | Denied | Denied (no regression) |
| CWD = anywhere, read CWD-relative path | Allowed | Allowed (no regression) |

## Verification steps

### 1. Unit tests (deterministic)

```
pytest tests/test_b11_stdlib_default_read_zone.py -v
```

6 new Tier 2 tests, all pass:
- `test_stdlib_path_in_default_read_zone_with_foreign_cwd` — core invariant
- `test_stdlib_path_readable_without_explicit_session_approval` — no approval needed
- `test_stdlib_default_zone_is_skill_agnostic` — all skills can read stdlib
- `test_stdlib_subtree_fully_in_default_zone` — deep nested paths included
- `test_non_stdlib_external_path_still_denied` — no false positives
- `test_stdlib_default_zone_cwd_still_works` — CWD zone not broken

### 2. Updated tests (stdlib-as-out-of-zone pre-conditions replaced)

`tests/test_g15_noninteractive_startup_guard.py` — 3 tests updated to use
`tmp_path_factory.mktemp()` instead of stdlib paths for "out-of-zone" examples.

`tests/test_skill_improver_stdlib_read_perm.py` — 2 tests updated:
- `test_is_read_allowed_for_skill_improver_with_session_approval` → renamed to
  `test_is_read_allowed_for_skill_improver_stdlib_in_default_zone` (pins new behavior)
- `test_is_read_allowed_skill_scoped_other_skill_denied` → updated to use external
  tmp dir for skill isolation test (stdlib is global default zone, not skill-scoped)

### 3. Full suite

```
pytest --tb=short -q
1022 passed, 2 xfailed in 69.71s
```

Pre-fix: 1016 passed. Post-fix: 1022 passed (+6 new). 0 failures.

## Circular import handling

The naive implementation called `stdlib_root()` at module load time, which
triggered a circular import:

```
permissions.py → reyn.user_intervention → reyn.__init__ → reyn.schemas.models
→ reyn.permissions.permissions (already loading) → ImportError
```

The `except Exception` clause silently caught this and returned `None`, meaning
`_STDLIB_READ_ZONE = None` and the fix had no effect.

Resolution: lazy initialization with `_STDLIB_READ_ZONE_RESOLVED` flag.
`_get_stdlib_read_zone()` is called on the first invocation of
`_in_default_read_zone`, by which time all modules are fully loaded.

## B11-NEW-1 status

**Resolved** at this commit.

The dominant blocker for `copy_to_work` preprocessor step[1] permission_denied
is eliminated.  Integration runs that reach `copy_to_work` should now proceed
past step[1].

Remaining blocker: B11-NEW-2 (router 60% text-reply non-determinism) — tracked
separately in B12-R2.
