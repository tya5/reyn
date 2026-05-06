# B13-R2 — Revert: stdlib_root() addition to default read zone (R1 / 2219b20)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| Reverted commit | `2219b20` (batch 12 R1) |
| main HEAD at dispatch | `8d6aff2` |
| Test count before | 1022 passed, 2 xfailed |
| Test count after | 1016 passed, 2 xfailed |
| Test delta | -6 (R1-specific test file deleted) |

## What was reverted

Commit `2219b20` (batch 12 R1) extended `_in_default_read_zone` in
`src/reyn/permissions/permissions.py` to include `stdlib_root()` as a second
default read zone alongside CWD.  It also added lazy initialization helpers
(`_STDLIB_READ_ZONE`, `_STDLIB_READ_ZONE_RESOLVED`, `_get_stdlib_read_zone()`)
to avoid a circular import at module load time.

This violates `docs/en/concepts/permission-model.md`:

> ### Layer 1: defaults
> Read/glob/grep anywhere under the project root.

= **default zone は project root のみ**。 stdlib path は layer 2 declaration +
layer 3 pre-approval で対応すべきで、 layer 1 (default) への追加は doc 違反。

## Revert scope

### Production code

`src/reyn/permissions/permissions.py`:
- Removed `_STDLIB_READ_ZONE`, `_STDLIB_READ_ZONE_RESOLVED` module-level variables
- Removed `_get_stdlib_read_zone()` helper function
- Restored `_in_default_read_zone` to single-zone logic: checks only `Path.cwd()` ancestry

### Tests

**Deleted** (6 tests):
- `tests/test_b11_stdlib_default_read_zone.py` — entire R1-specific test file
  (6 Tier 2 tests that pinned the now-reverted stdlib default zone behavior)

**Updated** (2 tests):
- `tests/test_skill_improver_stdlib_read_perm.py`:
  - `test_is_read_allowed_for_skill_improver_stdlib_in_default_zone` → restored to
    `test_is_read_allowed_for_skill_improver_with_session_approval` (pre-R1 name and
    behavior: stdlib path is OUT of default zone when CWD = tmp_path; session
    approval is required and tested)
  - `test_is_read_allowed_skill_scoped_other_skill_denied` → restored to use
    stdlib paths (not external tmp dirs) as the "out-of-zone" example, since
    stdlib paths are again outside CWD when CWD = tmp_path

Note: `tests/test_g15_noninteractive_startup_guard.py` was **not touched** — it
is being deleted by the sister sonnet (R1 revert for G15 / commit `651a053`).

## User-visible impact

**dogfood automation** (worktree-based `reyn chat` runs): stdlib path reads now
require layer 2 declaration + startup_guard approval again.  The documented
fix is `reyn.local.yaml` pre-approval (layer 3 mechanism):

```yaml
# reyn.local.yaml (operator personal config, gitignored)
permissions:
  file.read: allow
```

This matches documented design: `reyn.local.yaml` / `reyn.yaml` is how operators
pre-approve paths for non-interactive / CI runs.

**Production users**: unaffected.  Production users run `reyn chat` from their
project root (not a worktree), so CWD covers stdlib paths via CWD zone (same
as before R1 fix was introduced).

## Test count change

| Metric | Before revert | After revert |
|---|---|---|
| Passed | 1022 | 1016 |
| xfailed | 2 | 2 |
| Failed | 0 | 0 |
| Delta | — | -6 |

The -6 reflects the 6 R1-specific tests in `test_b11_stdlib_default_read_zone.py`
that are no longer valid (they tested the now-reverted behavior).  The 2 updated
tests in `test_skill_improver_stdlib_read_perm.py` replace their R1 versions
(net 0 change from those 2 files).
