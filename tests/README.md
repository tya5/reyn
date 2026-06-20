# tests/ — layout & conventions

The normative testing policy is **`docs/deep-dives/contributing/testing.ja.md`**
(English: `testing.md`). Read it before adding or changing tests — every test
declares exactly one Tier and its docstring's first line states it
(`"""Tier 2: ..."""`). This file only covers **where files go** and the
**shared-helper** convention.

## Where a new test goes

New tests go in a **subsystem subdirectory mirroring `src/reyn/`**, alongside
the existing `cli/`, `web/`, `gateway/`, `chat/` dirs:

```
tests/
  security/   ← src/reyn/security/   (permissions, secrets, sandbox)
  runtime/    ← src/reyn/runtime/    (router loop, session, budget, services)
  llm/        ← src/reyn/llm/
  core/       ← src/reyn/core/       (kernel, events, op_runtime)
  tools/      ← src/reyn/tools/
  skill/      ← src/reyn/skill/
  schemas/    ← src/reyn/schemas/
  services/   ← src/reyn/services/
  mcp/        ← src/reyn/mcp/
  ...         ← (one dir per src/reyn/<subsystem> as needed)
  cli/ web/ gateway/ chat/   ← already exist
```

Create the subdir on first use. There is **no `__init__.py`** in `tests/` or its
subdirs — the suite is collected from the rootdir as an implicit namespace
package (`pythonpath = ["src"]` in `pyproject.toml`).

### Issue-number suffixes are KEPT

Files keep their `_<issue>` provenance suffix
(e.g. `test_cwd_sandbox_aware_1477.py`). The suffix records *why* the test
exists and is intentionally preserved when files move into subsystem dirs. Do
not strip it.

### The ~700 flat top-level files: migration is DEFERRED

Most existing tests still sit flat at `tests/test_*.py`. **Do not bulk-move them
now** — a mass rename would collide with the many in-flight branches that add
test files concurrently. The flat→subsystem migration is a separate, staged
step done in small slivers. New tests should still follow the subsystem-dir
convention above; the backlog is migrated incrementally.

## Shared helpers → `tests/_support/`

Helpers needed by **more than one** test module live in `tests/_support/`, a
real package (`__init__.py`), **not** in a sibling test file:

| module                              | exports                                                            |
| ----------------------------------- | ------------------------------------------------------------------ |
| `tests._support.permissions`        | `make_resolver`                                                    |
| `tests._support.router_loop`        | `FakeRouterHost`, `FakeEventLog`, `ScriptedLLM`, `text_result`, `tool_result`, `make_loop`, `EMPTY_USAGE` |
| `tests._support.router_host_adapter`| `make_adapter` + inert `null_*` action ports                       |
| `tests._support.session`            | `make_session`, `push`, `now`, `synthetic_t_max`                  |

**Why not import from a sibling test module?** `from tests.test_foo import _bar`
binds the helper to a *test file's* location. That breaks the moment `test_foo`
is moved (the deferred migration above), and historically only resolved under
full-suite collection. `tests._support.<module>` is a stable, location-
independent path: helpers survive file moves and isolated collection alike.

`tests/_support/` contains **support code only** — no `test_*` functions; it is
never collected.

### Rules for `_support`

- It is still test infra: the testing policy applies (no `unittest.mock` —
  use real instances or the `LLMReplay` Fake; no private-state assertions in
  the helpers you build).
- `tests/scaffold/` is unchanged and remains the *only* home for bounded-life
  characterization/snapshot tests (see the policy's Annex). `_support` is for
  durable shared helpers, not scaffolding.

### `tests` is importable in any invocation style

`tests/conftest.py` inserts the repo root onto `sys.path`, so
`from tests._support... import X` resolves under `python -m pytest`, bare
`pytest`, a different cwd, or an IDE runner — not just full-suite collection
from the repo root.

## Known residual cross-test imports (next sliver)

Two force-close scenario modules still import scenario-specific builders from a
sibling test module rather than `_support` (the builders are tightly coupled to
single-use scaffolding, so extracting them is deferred to avoid churn in the
active force-close area):

- `test_force_close_reentry_integration_1092` → `test_routerloop_convergence_compaction_1092`
- `test_force_close_termination_1092` → `test_force_close_reentry_integration_1092`

Both still collect in isolation thanks to the conftest `sys.path` bootstrap;
moving their helpers into `_support` is a follow-up.
