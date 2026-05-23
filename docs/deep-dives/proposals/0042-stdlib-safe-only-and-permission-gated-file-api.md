# FP-0042: Stdlib safe-only + permission-gated file API for safe-mode python

**Status**: accepted — Phase 1 + 2.1 + 2.2 + 2.3 + 2.4 + 2.5 + 3 landed (2026-05-23)
**Proposed**: 2026-05-23
**Author**: dogfood-coder (sandbox_2 session)

**Landed PRs** (6-stack cascade):

| Phase | PR | Branch | Headline |
|---|---|---|---|
| 1 | #553 | `feat/reyn-safe-namespace-and-file-api` | `reyn.safe` public namespace + permission-gated `reyn.safe.file` |
| 2.1 | #557 | `feat/index-docs-migrate-to-safe-file` | `index_docs` preprocessor steps → safe-mode |
| 2.2 | #562 | `feat/index-docs-phase-2-2-write-chunks-safe` | `index_docs.write_chunks_with_lock` → safe + `safe.file.{mkdir,delete}` + `safe.process` |
| 2.3 | #566 | `feat/index-events-migrate-to-safe-mode` | `index_events` all 3 python steps → safe + `safe.file.write_atomic` |
| 2.4 | #573 | `feat/mcp-registry-safe-http` | `mcp_install` + `mcp_search` → safe via new `reyn.safe.mcp.registry` |
| 2.5 | #575 | `feat/eval-builder-safe-mode` | `eval_builder` legacy unsafe resolver deleted |
| 3 | (this PR) | `feat/fp0042-phase3-ci-guard-docs` | CI guard + docs doctrine + grandfathered exemption list |

**Deferred** (= follow-up issues):
- Permission granularity decomposition + reyn-internal HTTP/IO gating + generic `safe.http`: tracked at [Issue #571](https://github.com/tya5/reyn/issues/571).
- `ops_report` + `skill_improver` migration (= grandfathered as Phase 3 exemptions; would be Phase 2.6 / 2.7).
- `apply_strategy` retire decision (= deprecated compat path; pending the project-override survey).

---

## Summary

Today, several stdlib skills (= `index_docs`, `index_events`, `mcp_install`,
`mcp_search`, `eval_builder`) declare `mode: unsafe` python preprocessor /
postprocessor steps that call `reyn.api.unsafe.file` and execute raw
`open()` against arbitrary paths. By design (see the module's own docstring),
**unsafe-mode python bypasses Reyn's per-call permission resolver entirely**
— permission is granted once at step approval and never audited per
invocation. The dogfood batch B51 surfaced this in W2-S1 / W2-S7 as
`skill_run_failed` because `--allow-unsafe-python` was not provided.

The deeper concern, and the one this proposal targets: **stdlib should not
require unsafe-python to function**. Reyn ships these skills to all
operators; a "regular user who does not allow unsafe" should still be able
to run `index_docs`. Stdlib that demands unsafe contradicts the user
trust model Reyn promises.

This proposal:

1. Establishes `reyn.safe.*` as the **public surface** for safe-mode python
   helpers (= closes the gap where the allowlist allows the namespace but
   no module actually lives there).
2. Adds **`reyn.safe.file`** — a permission-gated file API that goes
   through the existing 4-layer permission resolver per call, exposing
   both a high-level (`read` / `write` / `glob`) and a low-level
   (`open` returning a real IO object) interface.
3. Defines a **migration path** for the five stdlib skills off
   `reyn.api.unsafe.file` onto `reyn.safe.file`, so stdlib runs entirely
   under the permission model it asks operators to trust.

After migration, `reyn.api.unsafe.*` remains for **user skills** that
genuinely need raw I/O (= explicit operator opt-in via
`--allow-unsafe-python`), but stdlib references go to zero.

---

## Motivation

### Current state

- `src/reyn/api/unsafe/file.py` `read()` is literally `with open(path) as f: return f.read()` — no permission check at the call boundary. The module docstring is honest:

  > "Permission was granted at parent level when the step's `mode: unsafe`
  > was approved at startup; individual calls are NOT audited per-invocation
  > (= step-level audit only). For finer audit see FP-0015 (deferred)."

- `src/reyn/kernel/_python_allowlist.py` already encodes the design intent:

  ```python
  if module_name == "reyn.unsafe" or module_name.startswith("reyn.unsafe."):
      return False
  if module_name == "reyn.safe" or module_name.startswith("reyn.safe."):
      return True
  ```

  But there is no `src/reyn/safe/` directory. The safe helpers that do
  exist live under `src/reyn/api/safe/` (= `time.py`, `random.py`, `hash.py`,
  `text.py`, `json.py`, `schema.py`), a path the allowlist does **not**
  match — top-level `reyn` is not in `PURE_STDLIB_ALLOWLIST`, so
  `reyn.api.safe.X` import in a safe-mode step is rejected.

- Net: safe-mode python today can import **only stdlib** plus whatever
  `extra_allowed` modules the operator passes — none of Reyn's own
  helpers are reachable. Stdlib skills compensated by declaring
  `mode: unsafe` and importing `reyn.api.unsafe.*` directly, bypassing
  the permission system that gates the rest of Reyn.

### Why this matters

The Reyn permission model (see `docs/concepts/permission-model.md`)
promises: file paths, shell, MCP, and Python steps are all gated through
the same 4-layer flow (config-deny → saved-grant → interactive-ask →
default). Inside an unsafe-mode python step, that promise is silently
suspended for file I/O. Operators who pre-approve `python.unsafe` for
stdlib are effectively pre-approving unrestricted file access. The
violation is internal (= stdlib trusting itself), but it muddies the
boundary the model presents to users.

It also blocks dogfood from running stdlib end-to-end without operator
opt-in (= B51 W2-S1/S7), which means we cannot measure what stdlib
actually does in a default-locked-down operator environment.

---

## Design

### Two changes that ship together

#### 1. `reyn.safe.*` public namespace

Create `src/reyn/safe/` as the **public** surface for Reyn helpers that
safe-mode python steps may import. The existing helpers under
`reyn.api.safe.*` are migrated (= moved, with thin re-export shims left
behind for one release) so the public path is the documented one and the
allowlist's existing match rule starts paying off.

Layout:

```
src/reyn/safe/
├── __init__.py      # re-exports + public surface contract
├── file.py          # NEW — permission-gated file API (see §2)
├── time.py          # migrated from reyn.api.safe.time
├── random.py        # migrated from reyn.api.safe.random
├── hash.py          # migrated from reyn.api.safe.hash
├── text.py          # migrated from reyn.api.safe.text
├── json.py          # migrated from reyn.api.safe.json
└── schema.py        # migrated from reyn.api.safe.schema
```

`src/reyn/api/safe/` keeps `from reyn.safe.X import *` shims for one
release so external skills depending on the old path keep building.
Remove in the release after.

No allowlist change is needed (= `_python_allowlist.py` already grants
`reyn.safe.*`). The migration just makes the design intent reachable
from real code.

#### 2. `reyn.safe.file` — permission-gated file API

A new module that exposes file I/O to safe-mode python steps **through**
the permission resolver, not around it. Two interface layers:

##### High-level (drop-in replacement for `reyn.api.unsafe.file`)

```python
from reyn.safe import file as sf

content = sf.read("docs/concepts/architecture.md")     # → str
sf.write(".reyn/tool-results/out.jsonl", payload)      # → None
paths = sf.glob("docs/**/*.md")                        # → list[str]
ok = sf.exists("src/reyn/safe/file.py")                # → bool
info = sf.stat("README.md")                            # → {size, mtime, mode}
```

Each call:

1. Resolves the path against the calling step's declared
   `permissions.file.read_paths` / `write_paths`.
2. Hands off to `PermissionResolver` for the 4-layer gate (config-deny /
   saved-grant / interactive-ask / default — same flow the chat router
   uses for `invoke_action(file__read)`).
3. On grant: performs the I/O.
4. On deny: raises `PermissionError`. The skill run fails with a clear
   `skill_run_failed` event carrying the permission denial, the same
   shape that op-runtime file ops produce.

##### Low-level (Python-IO-object compatible)

```python
with sf.open("docs/X.md", "r", encoding="utf-8") as f:
    for line in f:                # iterable ✓
        ...
    f.seek(0)
    head = f.read(4096)           # partial read ✓
    json.load(f)                  # stdlib lib compatible ✓
```

`sf.open(path, mode, encoding=...)` performs the same permission check
at open time, then returns a **real `io.TextIOBase` / `io.BufferedIOBase`**
(= the value `builtins.open` would have returned). Existing stdlib
libraries (`csv.reader`, `json.load`, `for line in f`) work unchanged.

This is the natural complement to safe-mode's banned-builtin list:
`builtins.open` is rejected at AST parse, `reyn.safe.file.open` is the
permission-gated gateway that takes its place.

##### Why both layers

- `read` / `write` / `glob` cover ~90% of stdlib chunker / indexer
  needs (= one-shot read, one-shot write, path enumeration) and keep
  the call site simple.
- `open` exists for cases where streaming or partial access matters
  (= reading a large JSONL line by line, passing a file-like to a
  parser that doesn't accept strings). It also unblocks library code
  that demands an `io.TextIOBase`.

##### Trade-off acknowledged

Once `sf.open(path)` grants access, byte-level granularity is gone —
the caller can `seek` / `read` arbitrary portions of the file. The
permission model is "may read this path", not "may read these bytes".
TOCTOU (= symlink swap between `open` and final `read`) is not addressed
here; if needed it lands as a follow-up after the basic API ships.

### Permission propagation

```
skill.md frontmatter declares:
  permissions:
    file:
      read_paths: ["docs/", "src/reyn/"]
      write_paths: [".reyn/tool-results/"]
              │
              ▼
PreprocessorExecutor reads the file-permission decl from the skill
              │
              ▼
PythonRunner launches the subprocess with the decl + a PermissionResolver
proxy (= IPC bridge if the resolver lives in the parent; in-process
delegate if the runner shares the parent's resolver)
              │
              ▼
reyn.safe.file.read(path) calls resolver.require_file_read(path)
              │   ┌─ allow → builtins.open(path) → return content
              └───┤
                  └─ deny  → raise PermissionError
                            (subprocess returncode → step failure event)
```

The subprocess-side `PermissionResolver` proxy is the only piece that
needs implementation choice: in-process (= python step runs in the same
process as the runner) is simpler and matches today's `python_runner`
behaviour. Out-of-process bridging is a later option if security
hardening demands it.

### Allowlist follow-up

`_python_allowlist.py` already allows `reyn.safe.*`. After migration:

- `reyn.api.safe.*` import in safe-mode python continues to work via the
  shim until removal.
- `reyn.api.unsafe.*` import in safe-mode python continues to be rejected
  by the existing top-level-not-in-stdlib path. The explicit
  `reyn.unsafe.*` reject branch can stay (= defence-in-depth for the
  reserved namespace) even though no module lives there.

---

## Migration plan

Phase 0 — design freeze (= this proposal accepted).

Phase 1 (= one PR):
- Create `src/reyn/safe/` with the public re-exports + `file.py`
  permission-gated API.
- Wire the permission propagation in `PythonRunner` /
  `PreprocessorExecutor` so a safe-mode step's `permissions.file.*`
  declaration reaches `reyn.safe.file.*` at call time.
- Unit tests: read/write within declared paths succeeds, outside denies
  with `PermissionError`, `open()` returns a real IO object that
  satisfies `io.TextIOBase`, stdlib `json.load(f)` works.

Phase 2 (= one PR per stdlib skill, smallest-first):
- `index_docs/chunkers.py`: `gather_samples`, `cost_preflight`, and the
  postprocessor `write_chunks_with_lock` → migrate to `reyn.safe.file`,
  switch `skill.md` steps to `mode: safe`, declare `read_paths` /
  `write_paths`.
- Dogfood scenario W2-S1 / W2-S7 should then succeed on the default
  operator profile (no `--allow-unsafe-python`).
- Repeat for `index_events`, `mcp_install`, `mcp_search`, `eval_builder`.

Phase 3 (= cleanup PR):
- `reyn.api.unsafe.file` import count in stdlib = 0. Add a CI check that
  fails if a new stdlib skill adds an unsafe-python step.
- Document the boundary in `docs/concepts/python-safe-mode.md`: stdlib =
  safe-only; user skills may opt into unsafe.

Phase 4 (= future, separate proposal):
- Per-call audit events for `reyn.safe.file.*` reads (= FP-0015 was the
  deferred precursor; can land cleanly once the safe API is the only
  surface stdlib uses).

---

## Out of scope

- Removing `reyn.api.unsafe.*` entirely. User skills (= `reyn/local/...`
  or `reyn/project/...`) that legitimately need raw file access retain
  the option. The operator opt-in via `--allow-unsafe-python` is the
  agreed boundary; this proposal does not move it.
- A general byte-range permission ("may read bytes 0–8192 of `X`").
  Path-level granularity is sufficient for stdlib needs.
- TOCTOU hardening (= reopening, fd-based handoff). If a hostile
  environment becomes a concern, it lands as a follow-up.
- The naming question "should the public namespace be `reyn.safe` or
  something else (`reyn.stdpy`?)". The allowlist already commits to
  `reyn.safe.*`; changing the name is a larger discussion than this
  proposal's scope.

---

## Open questions

1. **Shim duration.** How many releases should `reyn.api.safe.*` keep
   re-export shims to `reyn.safe.*`? Default proposal: one release with
   a deprecation warning, removed in the next. Adjustable.

2. **Per-call audit events.** Should `reyn.safe.file.read(path)` emit
   an event each invocation (= matching the op-runtime file_read
   event), or batch at step boundary? Per-call gives the cleanest
   audit but multiplies event volume on indexer skills that read
   thousands of files. Defer to Phase 4.

3. **`open` vs explicit context manager.** Should we offer only the
   `with sf.open(...)` shape and reject use without a context manager?
   Forces resource cleanup but is more restrictive than builtin `open`.
   Default proposal: match builtin's relaxed shape, document the
   `with` pattern as recommended.

4. **`reyn.safe.file.delete`.** The `reyn.api.unsafe.file` module
   exposes `delete()`. Should the safe surface? Currently stdlib only
   needs read + write + glob + exists + stat. Delete is a strong
   action; deferring it keeps the safe API minimal until a concrete
   stdlib need appears.

---

## References

- `src/reyn/kernel/_python_allowlist.py` — current safe-mode import contract.
- `src/reyn/api/unsafe/file.py` — current unsafe API + the honest docstring
  acknowledging the per-call audit gap.
- `src/reyn/api/safe/` — existing helpers that need to migrate to
  `reyn.safe.*`.
- `docs/concepts/permission-model.md` — the 4-layer permission flow this
  proposal extends into safe-mode python.
- `docs/concepts/python-safe-mode.md` — the safe-mode contract this
  proposal closes the implementation gap on.
- `docs/deep-dives/audits/2026-05-15-pure-mode-stdlib-audit.md` — the
  audit that previously split `extract_and_split` into `chunkers_safe.py`;
  this proposal generalises that pattern to a public API surface.
- Dogfood B51 retrospective (= `docs/deep-dives/journal/dogfood/2026-05-23-batch-51-sp-v18v19-plan-workspace/retrospective.md`)
  surfaced the W2-S1 / W2-S7 stdlib unsafe-block as the empirical trigger.
