---
type: proposal
topic: permissions
status: draft
created: 2026-05-24
---

# Phase 8 — `require_web_fetch` removal planning

Successor to the #571 collapse arc Phase 7 (PR #637). Phase 7 routed `web_fetch` through `require_http_get` and kept `require_web_fetch` as a legacy compat fallback for skills that haven't declared `http.get` yet. This proposal scopes the removal of that fallback once the migration window has elapsed.

## Current state (= post #637 + #641 + #643 v2)

`require_web_fetch` lives at `src/reyn/permissions/permissions.py:1320`. It is **unused in production code**:

- `web_fetch` op handler (= `src/reyn/op_runtime/web.py`) routes through `require_http_get` (#637).
- Legacy `web.fetch: allow / deny` config keys are honoured **inside `require_http_get`** as backward-compat aliases.
- DeprecationWarning fires only when a skill has no `http.get` declaration at all AND `web_fetch` is called.

Remaining references:

| Reference | Kind | Disposition |
|---|---|---|
| `PermissionResolver.require_web_fetch` | Method, async | **Delete** |
| Legacy `web.fetch: allow / deny` checks in `require_http_get` (5 sites) | Compat branch | **Delete** |
| DeprecationWarning + legacy prompt fallback in `require_http_get` no-decl branch | Compat branch | **Delete**; raise immediately instead |
| `tests/test_permission_prompt_phrasing.py::test_require_web_fetch_prompt_is_natural` | Test | Migrate to test the wildcard prompt instead |
| `tests/test_web_fetch_unified.py::test_require_web_fetch_config_*` (3 tests) | Tests | Migrate to test `require_http_get` legacy-compat-via-`web.fetch` behaviour (= until removal) → then update to `http.get` semantics |
| `dogfood/scripts/verify_permission_prompt_structure.py` | Trace tool | Update to call `require_http_get` |
| `cli/templates.py:60` template comment | Documentation | Update example to `permissions.http.get: [{host: "*"}]` |
| `chat/router_loop.py:300` `web.fetch: allow` catalog check | Catalog visibility | Verify whether the check should move to `http.get` config or stay on the legacy key |
| `chat/services/router_host_adapter.py:339` docstring | Docs | Update to reference `require_http_get` |
| `dispatch/dispatcher.py:511` error message | Error message | Update suggestion to `permissions.http.get: [{host: "*"}]` |
| Docs (`permission-model.md`, `mcp.md`, `reyn-yaml.md`) | Backward-compat notes | Remove "legacy alias" sentences once code is removed |

## Migration window criterion

`require_web_fetch` is safe to remove when **no in-the-wild skill relies on the legacy fallback path**. The signals we have:

1. **Stdlib coverage** (= already verified):
   - `skill_search`, `skill_importer` declare `http.get` explicitly (PR #618 Phase 3).
   - `mcp_install` skill declares `file.write` + `http.get` + `secret.write` (PR #631 Phase 5).
   - `chat_router` declares `http.get: [{host: "*"}]` wildcard (PR #637 Phase 7).
2. **Telemetry signal** (= proposed monitor):
   - Audit `.reyn/events/*.jsonl` for the `DeprecationWarning` content (= `"http.get not declared"`) over a release cycle.
   - Zero fires in production → safe.
   - Any fires → identify caller, request migration, re-evaluate.
3. **Legacy `web.fetch` config presence** (= less reliable signal):
   - Operators may still have `permissions.web.fetch: allow` in `reyn.yaml` from before Phase 7. The config key continues to work post-removal as a project-wide pre-approval **only if** we migrate it to `permissions.http.get: allow` in the same PR (= rename alias in `_is_config_approved` / `_is_config_denied`).
   - Or accept the config-key break as part of the migration (= operators rename `web.fetch` → `http.get`).

**Recommended trigger**: 1 release cycle after Phase 7 lands with no DeprecationWarning fires reported. Conservative; gives downstream skill authors time to notice the warning and update.

## Phase 8 PR scope

### Source changes

1. `src/reyn/permissions/permissions.py`:
   - Delete `require_web_fetch` method (= ~30 lines).
   - Strip legacy `web.fetch` compat branches in `require_http_get`:
     - `_is_config_denied("web.fetch")` check at top → keep only if we decide to keep `web.fetch` as a config-key alias for `http.get` (= operator-config backward compat, separate decision).
     - `_is_config_approved("web.fetch")` short-circuit → same decision.
     - `self._saved.get("web.fetch") or self._session.get("web.fetch")` host-blanket check → delete.
     - Wildcard 4-layer `_approve(key="http.get:<host>")` flow → keep (= this IS the new gate).
     - No-decl DeprecationWarning + legacy-`web.fetch` fallback prompt → **delete**; replace with immediate raise pointing at the `http.get` declaration.
   - Net: ~50-70 lines removed.

2. `src/reyn/cli/templates.py`:
   - Replace `# web.fetch: allow` template comment with `# http.get: [{host: "*"}]` example.

3. `src/reyn/chat/router_loop.py:300`:
   - Verify the `web.fetch: allow` catalog-visibility check. If `http.get` config key is the new home, switch reference.

4. `src/reyn/dispatch/dispatcher.py:511`:
   - Update error-message suggestion to point at `permissions.http.get`.

5. `dogfood/scripts/verify_permission_prompt_structure.py`:
   - Update the trace doc + check to call `require_http_get` instead.

### Test migration

1. `tests/test_permission_prompt_phrasing.py::test_require_web_fetch_prompt_is_natural`:
   - Replace with `test_require_http_get_wildcard_prompt_is_natural` (= test the wildcard 4-layer prompt's natural-language phrasing).

2. `tests/test_web_fetch_unified.py`:
   - 3 `test_require_web_fetch_*` tests → 2 of them (config-deny / config-allow) become regression tests for the **decision** whether `web.fetch` config key stays as alias. If kept, test against `require_http_get` directly. If removed, delete the tests.
   - 1 test (`test_router_invoke_action_web_fetch_deny_raises_permission_error`) is already passing post-PR #637 (= deny path still works via `require_http_get`). Verify regex stays correct.

3. `tests/test_permission_collapse_phase3.py`:
   - Remove the `test_require_http_get_no_decl_emits_deprecation_warning` test (= behavior being deleted).
   - Remove `test_require_http_get_legacy_web_fetch_allow_pre_approves` if `web.fetch` config alias is also removed.
   - Replace `test_require_http_get_raises_for_undeclared_host` to assert the new immediate-raise behaviour without DeprecationWarning.

### Doc updates

1. `docs/concepts/runtime/permission-model.md` + `.ja.md`:
   - Collapse arc section: mark Phase 7 → Phase 8 transition.
   - Remove "legacy fallback" sentences from `http.get` axis description.
   - If `web.fetch` config alias kept, document it as a backward-compat alias; if removed, remove all references.

2. `docs/reference/config/permissions.md`:
   - Web ops section: remove the `web.fetch: allow/deny` legacy-alias bullet if the key is removed.

3. `docs/concepts/tools-integrations/mcp.md` + `.ja.md`:
   - Enterprise pattern: drop `web.fetch: allow` example, use `http.get: [{host: "*"}]` everywhere.

## Open design question

**Should `permissions.web.fetch: allow / deny` config key stay as a backward-compat alias for `permissions.http.get`?**

- **Yes**: operator-config backward compat. `reyn.yaml` files in the wild that say `permissions.web.fetch: allow` keep working. Adds ~10 lines of alias-resolution code in `_is_config_approved` / `_is_config_denied`. Less disruptive.
- **No**: clean cut. Operators rename `web.fetch` → `http.get` in their config. More disruptive but ends with a smaller permission-config vocabulary.

Recommendation: **Yes (= keep alias)** through Phase 8. Schedule a Phase 9 cleanup that removes the config-key alias once `reyn.yaml` deprecation warnings (= a new addition in Phase 8) confirm no fires in production. This decomposes one disruption into two smaller ones.

## Sizing estimate

- Source: ~80 lines deleted, ~20 lines added (= raise message + config alias).
- Tests: ~50 lines moved / rewritten.
- Docs: ~30 lines updated.
- Total: **~6 files / ~150 lines**, single PR.

## Pre-conditions before opening Phase 8 PR

1. ✅ Phase 7 merged (= #637 — done).
2. ✅ Phase 7 follow-ups merged (= #634 / #640 / #641 / #643 — done).
3. ⏳ Migration window observation period: ≥ 1 release cycle elapsed.
4. ⏳ `.reyn/events/*.jsonl` audit shows no `DeprecationWarning` fires referencing `http.get not declared`.
5. ⏳ Stdlib + known user skills have all migrated to explicit `http.get` declarations (= already done for stdlib; user skills assumed migrated via DeprecationWarning visibility).

When (3) and (4) are satisfied, Phase 8 can be opened as a small follow-up PR.

## Out of scope

- Removing the `web.fetch` config key alias (= deferred to Phase 9 as noted above).
- Splitting `http.get` further (= per-method gating like `http.get` vs `http.post`). Phase 7 articulated this as YAGNI; nothing in Phase 8 changes that judgement.
- Touching the actual `web_fetch` op handler behavior — that's stable since #637.

## Related references

- `src/reyn/permissions/permissions.py:1320` — current `require_web_fetch` method.
- `src/reyn/permissions/permissions.py:937-1063` — `require_http_get` method including the legacy compat branches.
- `docs/concepts/runtime/permission-model.md` — "Collapse arc" section.
- PR #637 (Phase 7) — the unification work this proposal completes.
