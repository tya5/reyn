# B9-G15 Diagnosis — eval_builder stdlib path read permission gap

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| Giveup ref | G15 |
| Batch ref | B8-NEW-3 |
| Status | resolved (this document) |

## Hypotheses tested

Four hypotheses were provided by the task dispatcher:

| Hypothesis | Description |
|---|---|
| A | `startup_guard` requires interactive prompt; piped stdin → prompt fails → no approval |
| B | `startup_guard` not called for sub-skills (only top-level) → declarations unused |
| C | `is_read_allowed` wrong skill_name in stdlib path resolution |
| D | Some other timing / scope issue |

## Code paths examined

### `startup_guard` in non-interactive mode

`src/reyn/permissions/permissions.py:365`:
```python
async def _prompt_file_access(self, path, scope, skill_name, kind, bus):
    if not self._interactive:
        return False  # silently skips; records nothing in _session
```

When `interactive=False` (set by `sys.stdin.isatty()` in `src/reyn/cli/commands/run.py:182`),
`startup_guard` calls `_prompt_file_access` for each declared out-of-zone path, which
immediately returns `False` without recording any approval.  **Hypothesis A confirmed as
a necessary condition** — the prompt cannot be answered in non-interactive mode.

### `invoke_sub_skill` missing permission_resolver

`src/reyn/skill/sub_skill_runner.py:61-68`:
```python
agent = Agent(
    model=model,
    strict=False,
    subscribers=subscribers,
    resolver=resolver,
    intervention_bus=intervention_bus,
    caller=caller,
    # NO permission_resolver parameter!
)
```

The sub-skill `Agent` is created with `permission_resolver=None` (Agent's default).  This
means the sub-skill's `OSRuntime.workspace` has `self._perm = None`.

**Hypothesis B confirmed as the structural root cause** — the sub-skill has no resolver.

### Workspace deny path

`src/reyn/workspace/workspace.py:41-48` (`_resolve_read`):
```python
if resolved.is_relative_to(self.base_dir):
    return resolved
if self._perm and self._perm.is_read_allowed(str(resolved), self._skill_name):
    return resolved
raise PermissionError(f"read not permitted: {path!r} (outside project)")
```

When `self._perm is None`, any path outside CWD raises `PermissionError`.  The LLM
receives `{"status": "denied"}` and emits a `permission_denied` event — matching the
18 events observed in B8-S1.

### Hypotheses C and D

Not the primary cause.  `is_read_allowed` correctly uses the passed `skill_name`, and
there are no timing / scope issues.  The root cause is structural (no resolver in
sub-skill).

## Verdict

**Primary root cause: Hypothesis B** (sub-skill has no permission resolver).  
**Contributing factor: Hypothesis A** (startup_guard silently skips non-interactive prompts, so even if the sub-skill DID have a resolver, no approvals would have been recorded by the parent's startup_guard in non-interactive mode).

Both must be fixed together for the chain to work end-to-end:
1. `startup_guard` must record approvals in non-interactive mode (instead of silently returning False)
2. `invoke_sub_skill` must propagate the parent's PermissionResolver to the sub-skill

Note: commit `f229f6c` correctly added the permission declarations to `eval_builder/skill.md`,
but those declarations are only acted upon when a PermissionResolver is present AND startup_guard
records the approvals.  The declarations were necessary (and are still used), but not sufficient.

## Fix design

### Fix 1 — Non-interactive auto-approve declared file.read paths

In `startup_guard`, when `not self._interactive`, instead of calling `_prompt_file_access`
(which returns False immediately), call `session_approve_path` directly for each declared
file.read path.  This records the approval as session-only (not persisted).

Justification: the skill author explicitly declared the paths in checked-in `skill.md`.
The user opted in by invoking the skill (`reyn run <skill>`).  This is analogous to how
`--allow-untrusted-python` is the explicit opt-in for Python steps — except for file.read
the declaration itself is the explicit opt-in by the skill author.

Write paths and Python steps are intentionally NOT auto-approved in non-interactive mode:
- `file.write` to out-of-zone paths should require explicit config or persisted approval
- Python trusted steps still require `--allow-untrusted-python`

**Trade-off considered**:
- Option 2 (`--auto-approve-paths` flag) rejected: requires callers (sub-skill spawns) to
  thread the flag through the call chain, which is more complex with no gain since the skill
  declaration already provides the equivalent signal.
- Option 3 (propagate resolver) is necessary but not sufficient without Fix 1 — the parent's
  resolver has no approvals in non-interactive mode.
- Option 4 (auto-approve declared paths) applied but scoped to file.read only (not blanket).

### Fix 2 — Propagate PermissionResolver to sub-skills

Add `permission_resolver: PermissionResolver | None = None` to `invoke_sub_skill` signature
and pass it to `Agent(...)`.  Update `run_skill.py` handler to pass `ctx.permission_resolver`.

This ensures the sub-skill's workspace has a resolver and can check approvals recorded by
the parent's (and sub-skill's own) `startup_guard`.

`startup_guard` IS called for the sub-skill (OSRuntime.run line 1294 checks `if self._perm`).
With the resolver now present, the sub-skill's startup_guard runs Fix 1's non-interactive
path and records approvals for the sub-skill's declared paths under the sub-skill's name.

## Files changed

| File | Change |
|---|---|
| `src/reyn/permissions/permissions.py` | Fix 1: non-interactive auto-approve in `startup_guard` |
| `src/reyn/skill/sub_skill_runner.py` | Fix 2: add `permission_resolver` parameter |
| `src/reyn/op_runtime/run_skill.py` | Fix 2: pass `ctx.permission_resolver` to `invoke_sub_skill` |
| `tests/test_g15_noninteractive_startup_guard.py` | 7 Tier 2 tests pinning the fix |
