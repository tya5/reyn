# FP-0014: Python Step API Package + Rename Modes (pure→safe, trusted→unsafe)

**Status**: **proposed**
**Proposed**: 2026-05-11
**Author**: 2026-05-11 design discussion (post R-PURE-MODE-REDEFINE Step 1)
**Trigger**: Step 1 (commit `18f4aaa`) formalized pure mode as "ambient
sources only" but left three structural problems unresolved: (a) stdlib
still declares `mode: trusted` in 7 skills, contradicting the spec; (b)
the name `trusted` is paradoxical (Reyn trusts the author MORE, i.e.
verifies LESS — opposite of intuitive reading); (c) the natural fix
(= add I/O capabilities) is to grow `run_op` kinds, which proliferates
the YAML DSL surface line-by-line. This proposal supersedes
R-PURE-MODE-REDEFINE Step 2 with a redesigned approach.

---

## Summary

Three coupled changes landed in **one clean-break commit** (no two-phase
migration; current users are us only):

1. **Rename**: `pure` → **`safe`** and `trusted` → **`unsafe`**. Adopts
   the Rust precedent. `safe` = Reyn provides verified guarantees; `unsafe`
   = author asserts responsibility. `pure` is FP jargon and conflates with
   "no side effects" (= confusing since `random` / `time` are allowed);
   `trusted` is paradoxical (= Reyn trusts more = verifies less).
2. **API packages**: ship `reyn.safe.*` (callable from `safe` mode) and
   `reyn.unsafe.*` (callable from `unsafe` mode) as Reyn-provided Python
   packages. `reyn.unsafe.*` helpers are thin wrappers over the existing
   run_op dispatch (= permission gate + event emit + LLMReplay capture
   inherit for free). Author writes Python with type hints + autocomplete +
   docstrings instead of YAML declarative steps.
3. **`run_op` kind consolidation**: with end-user surface moving to API
   packages, run_op descends to **internal primitive layer**. Consolidate
   shape-overlapping kinds (e.g. `file_read` / `file_write` / `file_delete`
   / `file_glob` → single `file` op with verb+path+scope params) since
   nothing outside Reyn-internal calls them anymore.

**Stdlib outcome**: after this lands, stdlib has **zero `mode: unsafe`
declarations**. All stdlib python steps run in `safe` mode and invoke I/O
via `reyn.unsafe.*` packages — except the packages themselves are
not "unsafe-importable" from safe mode, which sounds like a contradiction.
The resolution: the OS imports run_op primitives directly (no python step
involved), and stdlib python steps that need I/O are refactored so the
I/O moves OUT of python into a separate `run_op` step in the
preprocessor chain. **`reyn.unsafe.*` is for USER unsafe-mode steps**, not
for stdlib (= which stays pure-safe by construction).

---

## Motivation

### Step 1 left three structural debts

Step 1 (commit `18f4aaa`) gave pure mode a clean author-facing definition
("ambient sources only") but the spec doesn't yet match reality:

1. **Stdlib still uses `mode: trusted`** in 7 skills (mcp_search,
   mcp_install, eval_builder, index_docs ×3, skill_improver). The plan
   file's Step 2 audit classified these into Class A (1 real I/O case),
   Class B (6 cases where I/O can split out and python stays pure), and
   Class C (4 mis-labeled pure functions). Until this lands, "stdlib
   auto-trust" and "trusted is the default escape hatch" are operationally
   indistinguishable — the spec doc lies about reality.

2. **The name `trusted` is paradoxical**. In Reyn's model:
   - `pure` = Reyn statically verifies safety (= safer for the operator)
   - `trusted` = Reyn trusts the author NOT to do anything dangerous
     (= less verification, more responsibility on the author)

   Reading `trusted` naively suggests "more trusted = safer" but the
   semantic is the opposite. Authors (and operators reviewing skills)
   mis-route their attention.

3. **Adding I/O capabilities tempts `run_op` proliferation**. Today's
   run_op family already has file_read / file_write / file_delete /
   file_glob / web_fetch / shell / iterate / validate / lint_plan /
   python. Each addition touches: schema model, linter knowledge,
   events schema, Control IR JSON shape, docs. Linear DSL surface
   growth — bad scaling.

### Why API packages beat run_op proliferation

Today the design splits "deterministic compute" (= python step) from
"effectful I/O" (= run_op step) at the **YAML DSL layer**. Authors stitch
multiple step types together. Adding capabilities means adding step
types.

The alternative: split them at the **Python API layer** instead. The
step type is just `python`; what the step can do is determined by which
Reyn-provided package it imports. The mode declaration (`safe`/`unsafe`)
governs which imports the AST validator permits.

| Axis | run_op expansion | Python API package |
|---|---|---|
| Author UX | YAML declarative step per I/O kind | Python imports + function calls (= type hints, docstrings, autocomplete) |
| Permission gating | run_op dispatch layer | **AST validator rejects forbidden import at parse time** + same permission check at function call time (reuses PermissionResolver) |
| Audit / events | run_op emits event | API function calls run_op dispatch internally → events emit for free |
| LLMReplay | run_op-recorded | same dispatch → same recording |
| Reyn evolution | YAML schema bump + linter update | **Python package version bump** only |
| Doc | per run_op kind reference page | `help(reyn.unsafe.file)` + sphinx-able |
| Spec enforcement | string-match allowlist | AST + import resolution = deterministic |

**The trick**: `reyn.unsafe.file.read(path)` is a **thin wrapper** over the
existing `file_read` run_op dispatch. Permission checks, event emission,
replay capture, error envelope — all reuse existing infrastructure. Zero
duplication. run_op kinds descend to internal primitives.

**P3 boundary**: this does not violate P3 (= "OS doesn't run things, only
Skills do"). The python step body is still author-written; Reyn's package
is just a vetted helper layer the author imports voluntarily. The LLM
does not write python steps — authors do (= user / stdlib).

### Why rename both modes together

Half-renaming (`trusted` → `unsafe` while keeping `pure`) leaves the pair
asymmetric and continues confusing newcomers — `pure` is FP-jargon, `safe`
is plain English. Symmetric `safe`/`unsafe` is the Rust-style pair every
working dev recognizes within a second. Mental model:

- **`safe`** = Reyn provides verified safety guarantees (AST allowlist,
  banned builtins, subprocess sandbox, ambient sources only).
- **`unsafe`** = Author asserts responsibility for what the step does.
  Reyn lifts the guarantees and runs the code as written.

Same semantics as Rust's `unsafe { ... }`: not "this is dangerous,"
but "I, the author, am taking responsibility for invariants the compiler
can't check."

### Clean break

Current external users: zero. The current `pure` / `trusted` keywords
have no production install base outside this repo. Standard 2-step
deprecation arc (= warn → reject) is pure overhead. **One commit:
rename, refactor stdlib, ship API packages, hard-reject old keywords in
linter, update docs.** Any in-flight branches with old YAML need a 5-line
fix on rebase.

---

## Proposed implementation

### Component A — Mode rename (mechanical)

Touch points (= grep `trusted` + `pure` in schema/permission code):

- `src/reyn/schemas/models.py::PythonStep` — `mode: Literal["pure", "trusted"]` → `Literal["safe", "unsafe"]`
- `src/reyn/permissions/permissions.py::PythonPermission` — same field rename
- `src/reyn/permissions/permissions.py` — permission keys `python.pure` / `python.trusted` → `python.safe` / `python.unsafe`
- CLI flag: `--allow-untrusted-python` → `--allow-unsafe-python`
- env var equivalents: `REYN_ALLOW_UNTRUSTED_PYTHON` → `REYN_ALLOW_UNSAFE_PYTHON`
- `_python_allowlist.py` comment + module docstring
- `docs/concepts/python-pure-mode.{md,ja.md}` → rename file + content sweep
- `docs/concepts/python-unsafe-mode.{md,ja.md}` — new pair doc
- `docs/guide/for-skill-authors/add-a-python-preprocessor.{md,ja.md}` — sweep
- `docs/guide/for-skill-authors/glossary.md` — sweep
- `docs/guide/for-users/manage-permissions.{md,ja.md}` — sweep
- `docs/reference/dsl/preprocessor.{md,ja.md}` — sweep
- All stdlib skill yaml: `mode: trusted` → either removed (= refactored to safe + run_op) or `mode: unsafe` (= unlikely to remain after refactor)
- Test fixture sweep
- `reyn lint` hard-rejects `mode: pure` and `mode: trusted` with clear migration message

### Component B — `reyn.safe` package

Shipped under `src/reyn/api/safe/`. Importable from `safe`-mode python
steps. Wraps stdlib + provides safe-mode-friendly helpers that the bare
allowlist doesn't include.

```python
# src/reyn/api/safe/__init__.py
"""Reyn-provided helpers callable from `safe`-mode python steps.

Every function in this package is provably ambient: output is
determined only by inputs + clock + entropy + bundled static data.
The AST validator allows `import reyn.safe.*` from `safe` mode.
"""

from . import hash, schema, text, time, random, json
```

Initial surface (= can grow):

- `reyn.safe.hash` — `sha256(b)`, `md5(b)`, `blake2b(b)`, file-content hash helpers
- `reyn.safe.schema` — `validate(data, schema)` (jsonschema), `assert_type(...)`
- `reyn.safe.text` — `regex.findall_named(...)`, `template.render_safe(...)` (= no Jinja escape hatches)
- `reyn.safe.time` — `monotonic_seq()` (= ambient clock helper that's explicit about non-determinism)
- `reyn.safe.random` — `seeded(seed)` (= ambient entropy with explicit seeding)
- `reyn.safe.json` — `loads_strict(s)` / `dumps_canonical(d)` (= sort_keys, ensure_ascii)

**No I/O.** No file, no http, no shell, no env, no time-as-source (= use
`reyn.safe.time.monotonic_seq()` if needed, which logs the read).

### Component C — `reyn.unsafe` package

Shipped under `src/reyn/api/unsafe/`. Importable from `unsafe`-mode
python steps (= AST validator allows).

**Scope A (this FP — default)**: each helper is a vetted thin wrapper
over stdlib I/O, executing **inside the python step's subprocess**.
Permission was already granted at parent level when `mode: unsafe` was
approved for the skill at startup; individual calls are NOT audited
per-invocation (= step-level audit only — same granularity as today's
`mode: trusted` direct `open()`). The win is namespace + type hints +
autocomplete + docstrings, not finer audit.

```python
# src/reyn/api/unsafe/file.py — runs INSIDE the python step's subprocess
def read(path: str, *, encoding: str = "utf-8") -> str:
    """Read a file.

    Runs in the python step's subprocess. Permission for filesystem
    access was granted when `mode: unsafe` was approved for this skill
    at startup; individual reads are NOT audited per-call (= step-level
    audit only). For finer audit see FP-0015 (deferred).
    """
    with open(path, encoding=encoding) as f:
        return f.read()

def write(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write a file. Step-level audit (= same as Scope A above)."""
    with open(path, "w", encoding=encoding) as f:
        f.write(content)

def glob(pattern: str) -> list[str]:
    """Glob match."""
    import glob as _glob
    return sorted(_glob.glob(pattern, recursive=True))
```

Initial surface:

- `reyn.unsafe.file` — read / write / delete / glob / exists / stat
- `reyn.unsafe.http` — get / post / put / delete (= JSON body convention, auto-encode, wraps `urllib.request`)
- `reyn.unsafe.shell` — run(argv, cwd=, env=) → CompletedProcess-like (= wraps `subprocess.run`)
- `reyn.unsafe.workspace` — path() / cwd() / list_artifacts() (= ergonomic workspace access)
- `reyn.unsafe.env` — get(key) (= explicit env read)

**Scope B (deferred — FP-0015)**: bidirectional RPC channel from child
back to parent's run_op dispatcher gives per-call audit (permission
re-check, event emission, LLMReplay capture per invocation). Out of
scope here; pointer in `Related`. Scope A leaves the namespace (=
`reyn.unsafe.*`) as the future hookup point — Scope B replaces the
wrapper bodies without breaking author-visible API.

### Component D — `run_op` kind consolidation

With user surface moving to API packages, run_op kinds descend to
internal primitives. Consolidate where shapes overlap:

| Before (multiple kinds) | After (single parameterised kind) |
|---|---|
| `file_read` / `file_write` / `file_delete` / `file_glob` | `file` (with `verb: "read"\|"write"\|"delete"\|"glob"`) |
| (future: http_get / http_post / ...) | `http` (with `method:`) |
| `shell` | `shell` (unchanged) |

Migration: in same commit, IR shape migrates + all stdlib skill yaml
+ Control IR producers (= the LLM-driven phase output) updated.
LLMReplay fixtures get regenerated (= acceptable, current cache scope
is the whole repo).

### Component E — Stdlib refactor (= former Step 2)

All 7 stdlib skills currently declaring `mode: trusted` get refactored:

- **Class A (1 case)**: `index_docs/apply_strategy` writes files + uses
  locks. Move the file write + lock acquire into a `file` run_op step
  earlier in the chain; python step receives the lock state as input,
  performs deterministic transformation, returns content; another
  `file` run_op step writes it. Net: no python I/O.
- **Class B (6 cases)**: registry fetch / analyzer / cost preflight /
  copy_to_work_resolver — I/O part splits to dedicated run_op step
  (HTTP fetch, file glob, etc.), python becomes dict manipulation.
- **Class C (4 functions)**: `skill_improver/copy_to_work.py`'s pure
  functions — drop the `mode: trusted` declaration, run as `safe`.

**Acceptance criterion**: `grep -r "mode: unsafe" src/reyn/stdlib` returns
**zero results** after the commit. Linter enforces this as a hard rule
for the stdlib path prefix.

### Component F — Lint enforcement

`reyn lint` adds three new rules:

- **`unsafe-in-stdlib`** — hard error. Stdlib skill declaring `mode:
  unsafe`. Message: "Stdlib skills must run in safe mode. Move I/O to
  a run_op step or `reyn.unsafe.*` package call from a user skill."
- **`unsafe-without-justification`** — warn. User skill declaring `mode:
  unsafe` without a `# justification:` comment within 3 lines. Message:
  "unsafe mode disables Reyn's safety guarantees. Add `# justification:
  <reason>` to document why unsafe is required."
- **`legacy-mode-keyword`** — hard error. `mode: pure` or `mode: trusted`
  detected. Message: "Renamed in FP-0014: pure → safe, trusted →
  unsafe. Update your skill.md."

---

## Open design questions (delegate to ADR)

These warrant follow-up ADRs once this proposal is accepted in principle:

1. **ADR-A: API package surface stability.** What versioning strategy
   protects `reyn.safe.*` / `reyn.unsafe.*` consumers from breakage?
   Semver against the package independent of Reyn core? Or pinned to
   Reyn version?
2. **ADR-B: Audit granularity (Scope A vs Scope B).** **Resolved by
   construction**: `python_runner.py` invokes
   `reyn.kernel._python_harness` as a `subprocess.run` child — contextvars
   and ambient dispatchers do NOT cross the process boundary. The
   parent's `dispatch_op` is unreachable from the child without an
   explicit bidirectional RPC channel.

   This FP adopts **Scope A**: `reyn.unsafe.*` helpers run inside the
   subprocess and call stdlib I/O directly. Permission gate is parent-
   side step-level (= existing `python.unsafe` permission grant at
   startup), event emit is step-level (`python_started` /
   `python_completed`). **Same audit granularity as today's `mode:
   trusted` direct `open()` — no regression**, with namespace + type
   hints + autocomplete + docstrings as net wins.

   **Scope B (= per-call audit via bidirectional RPC)** is deferred to
   **FP-0015**. Trigger: enterprise audit requirements that need
   per-invocation gating / events. Scope A leaves `reyn.unsafe.*` as
   the future hookup point — Scope B replaces wrapper bodies without
   breaking the API surface authors depend on.
3. **ADR-C: `run_op` consolidation scope.** `file_*` → `file` is
   straightforward. `iterate` / `validate` / `lint_plan` are different
   shapes — keep separate or consolidate? `python` itself is a run_op
   kind; keep that as the entry point or rename.
4. **ADR-D: User-facing `reyn.safe.time` semantics.** `time.monotonic()`
   today is in the safe allowlist but it's an ambient source the LLM
   replay can't reproduce deterministically. Should `reyn.safe.time`
   wrap it with a logged read (= recorded in events, replayable)?
5. **ADR-E: Future external user migration.** Even though current users
   are us only, post-1.0 the package surface becomes part of the public
   API. Decide BEFORE 1.0 whether `reyn.safe.*` / `reyn.unsafe.*` ship
   under that namespace or under a more conservative one (`reyn.sdk.*`,
   `reyn.runtime.*`, etc.).
6. **ADR-F: Trusted-mode `--allow-unsafe-python` consent UX.** Today
   the flag is one-shot per `reyn run`. With API packages, every
   `import reyn.unsafe.X` is a permission-gated decision. Does the flag
   gate the entire run, the import, or each call? Current design:
   import-level gate (= flag enables the import; permission grant
   covers individual calls per skill).

---

## Dependencies

- **R-PURE-MODE-REDEFINE Step 1 (LANDED 2026-05-11, commit `18f4aaa`)** —
  provides the "ambient sources only" definition this proposal builds on.
- **PR37 unified dispatch (LANDED)** — `dispatch_tool` provides the
  permission gate + event emit infrastructure the API packages reuse.
- **ADR-0020 skill-only permissions (LANDED)** — `permissions:` field on
  Skill not Phase; API package permission gating reuses this.

No new external dependencies.

---

## Migration plan (single commit)

No phased rollout — clean break in one commit, current users (= us) take
the 5-line yaml fix on rebase.

1. Rename schema field + permission keys + CLI flag + env vars.
2. Ship `reyn.api.safe` + `reyn.api.unsafe` packages with initial surface
   (Scope A: subprocess-local stdlib wrappers).
3. Refactor 7 stdlib skills (= drop `mode: trusted`, move I/O appropriately).
4. Consolidate `file_*` run_op kinds → single `file` op (= ADR-C resolution).
5. Update `reyn lint` with 3 new rules.
7. Docs sweep: rename concept doc, write `python-unsafe-mode.md` pair,
   add API package reference, update glossary / preprocessor doc /
   manage-permissions doc. EN + JA mirror.
8. Test sweep: regenerate fixtures, update assertions, add coverage for
   new API package wrappers.
9. ADR drafting for the 6 open questions (= as needed during implementation).

---

## Cost estimate

**MEDIUM** (~4.5 days focused work, parallelisable).

| Item | Estimate |
|---|---|
| Mechanical rename (schema + permissions + CLI flag + env vars + tests) | ~0.5 day |
| `reyn.safe` + `reyn.unsafe` packages + wrapper tests (Scope A) | ~1 day |
| Stdlib 7-skill refactor | ~1 day |
| Linter rules | ~0.5 day |
| run_op kind consolidation (`file_*` → `file`) + IR migration | ~0.5 day |
| Docs sweep EN+JA (concept docs + glossary + preprocessor + manage-permissions + reference) | ~0.5 day |
| Dogfood verify (= mcp_install / index_docs / skill_improver e2e) | ~0.5 day |
| ADR drafting (A, C–F, as needed; ADR-B resolved in this FP) | ~0.5 day |

**Total: ~4 days** (= refined from ~4.5d after ADR-B was resolved by
construction; dispatch wiring item dropped).

Sonnet-parallelisable: rename + docs sweep + linter rules + stdlib
refactor are largely independent. Stdlib refactor is the critical path
(= depends on rename + API package landing).

---

## Risks

- ~~**Dispatch context resolution (ADR-B)**~~ — **resolved by
  construction**. Subprocess boundary makes contextvars-based
  dispatch impossible without bidirectional RPC; this FP adopts
  Scope A (= subprocess-local helpers, step-level audit, same as
  today's `mode: trusted`). Scope B (= per-call audit) is deferred
  to FP-0015.
- **Stdlib refactor reveals missing run_op primitives.** Once the 7
  skills are audited in detail, the Class A case (index_docs file
  write + lock) may need a new run_op primitive (file lock acquire/
  release semantics) that doesn't exist today. Scope add of ~0.5 day
  if so.
- **API package surface freezes the contract early.** Post-1.0 changes
  to `reyn.safe.*` / `reyn.unsafe.*` are breaking changes for any
  external skill author. ADR-A and ADR-E need decisive answers BEFORE
  1.0 ships, not after.
- **Linter false-positives on legacy keywords.** Need careful
  string-match scope (= only inside skill yaml, not inside python
  source) so `# trusted by user` comments in code aren't tripped.

---

## Related

- **R-PURE-MODE-REDEFINE Step 1 (commit `18f4aaa`)** — author-facing
  definition of pure mode this proposal builds on (renamed to safe).
- **R-PURE-MODE-REDEFINE Step 2 (plan file residual)** — **superseded
  by this FP**. Step 2's stdlib refactor scope is folded in as
  Component E.
- **FP-0015 (deferred)** — Per-call audit via bidirectional RPC
  (= Scope B from ADR-B above). `reyn.unsafe.*` wrapper bodies switch
  from subprocess-local stdlib calls to dispatched RPC to the parent's
  `dispatch_op`. Author-visible API unchanged; audit granularity
  improves from step-level to per-call. Trigger: enterprise audit
  requirements.
- **ADR-0020 skill-only permissions (commit `7b93025` / `3dab751`)** —
  permission declaration unit this proposal reuses.
- **PR37 unified dispatch (commit `d06cb94`)** — dispatch + permission
  + event infrastructure the API packages call through.
- **`docs/concepts/python-pure-mode.{md,ja.md}`** — will rename to
  `python-safe-mode.{md,ja.md}` during the commit; content evolves
  to add the API package section.
- **Rust `unsafe { ... }` convention** — semantic inspiration for the
  rename. The mental model "author asserts responsibility for invariants
  the compiler can't check" transfers exactly.
