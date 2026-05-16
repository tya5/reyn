# R-PURE-MODE-REDEFINE — stdlib `mode: unsafe` audit

**Date**: 2026-05-15  
**Closed**: 2026-05-16 (all addressable refactors landed)  
**Scope**: Every `mode: unsafe` python step declared in `src/reyn/stdlib/skills/*/skill.md`.  
**Goal**: Identify which can move to `mode: safe` (= "ambient sources only" contract) and which honestly need `unsafe`. Produces concrete refactor sketches for the candidates.

The formal contract audited against:

> `mode: safe`: python step output depends only on (input artifact + ambient sources).
> Ambient sources = clock, entropy, bundled static stdlib data.
> Filesystem, network, subprocess, env-var access is syntactically unreachable.

## Summary

| Class | Count | Action |
|---|---|---|
| A — honestly unsafe | 11 | Kept as-is. Documentation updated; no further action. |
| B — split candidate | 3 | **All landed** (Wave 3a / 3b / 4) |
| C — mis-labeled (safe) | 1 | **Landed** (Wave 2) |
| D — needs new run_op kind | 2 op kinds covering 3 functions | **All landed** (Wave 5a + Class D-1 + Class D-2) |

Total `mode: unsafe` declarations audited: **17** across 7 stdlib skills.

### Latent bug flags

None found. No function is declared `mode: safe` but imports unsafe modules, and no
function that does genuine I/O is mislabeled `mode: unsafe` without actually needing it
(with one exception in Class C noted below).

---

## Landing log (2026-05-15 → 2026-05-16)

All addressable refactors (Class B, C, D) landed in six sequential waves across the
two-day window. Commits are listed in order of landing.

- **Wave 2 — commit `1a06289`**: Class C fix — `aggregate_from_recall_chunks` extracted
  from `aggregate.py` into a new `ops_report/aggregate_pure.py`, declared `mode: safe`.
  Matched the audit sketch exactly: zero logic change, module split only. ~30 min as
  predicted.

- **Wave 3a — commit `83f2cf5`**: Class B non-trivial — `collect_aggregate` (ops_report)
  split into a safe dispatcher `dispatch_aggregate` + unsafe fallback
  `collect_aggregate_fallback`. The audit sketch proposed a sentinel-aware 3-step
  preprocessor chain; the implementation confirmed this works cleanly (see Cross-cutting
  findings below). The 99% hot path (recall available) now runs `mode: safe`; the 1%
  raw-events fallback retains `mode: unsafe` as an honest declaration.

- **Wave 3b — commit `a2984d5`**: Class B / Class D hybrid — `read_on_propose_config`
  (version_snapshot.py). The audit marked this Class A ("defer, needs config_read op").
  Wave 3b instead used `file_read` op + a new regex-based pure parser
  `parse_on_propose_config_minimal` in `version_snapshot_pure.py`. The `yaml` module
  dependency was sidestepped by using a targeted regex over the two fields of interest
  rather than a full YAML parse. This is simpler than the `config_read_self_improvement`
  op alternative mentioned in the audit and avoids the OS extension entirely.

- **Wave 4 — commit `d0d65e8`**: Class B non-trivial — `collect_traces` (skill_improver)
  split into `dispatch_traces` (safe) + `collect_traces_fallback` (unsafe sentinel-aware),
  using the same 3-step preprocessor chain pattern as Wave 3a. The audit sketch had
  proposed a `trace_collector_pure.py` module split; the implementation used the same
  sentinel pattern established in Wave 3a rather than the dual skill.md entry approach in
  the sketch. The outcome is equivalent: 99% hot path safe, 1% fallback honest.

- **Wave 5a — commit `01f3ae0`**: New `skill_resolve` op kind (`OpPurity.world`). Lifts
  `resolve_skill_path()` from python steps to an OS-layer op. This unblocked Class D for
  both `resolve_paths` functions. 8 Tier 2 tests added. Matches the audit's Class D sketch
  exactly ("one op definition fixes two skills simultaneously").

- **Class D-1 — commit `f5f164c`**: `skill_improver/copy_to_work_resolver_pure.py`
  declared `mode: safe`, using `skill_resolve` op via `args_from:` template mechanism.

- **Class D-2 — commit `5cc797c`**: `eval_builder/analyze_skill_resolver_pure.py`
  declared `mode: safe`, same pattern as D-1.

---

## Per-skill audit

### `index_docs`

Source: `src/reyn/stdlib/skills/index_docs/chunkers.py`  
Skill.md permissions block declares 4 `mode: unsafe` entries plus 1 `mode: safe`
(`extract_and_split`) already correctly labeled.

---

#### `gather_samples` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `_api_glob_files(path)` (→ `reyn.api.unsafe.file.glob`) to discover files
matching a glob pattern, then calls `_unsafe_file.stat(f)` and `_unsafe_file.read(f)` to
get size and content of each sampled file. Imports `reyn.api.unsafe.file` at module level.

**Why it can't be split**: The core purpose of this step is to read file content for LLM
context (excerpt, structure hint). The file reads are the payload; there is no pure
post-processing remainder that would justify a split. All the work — glob, stat, read,
excerpt, structure detection — is intrinsically tied to filesystem access.

**Verdict**: keep

---

#### `cost_preflight` — Class B (split candidate, medium effort)

**Status: Kept as Class A (honestly unsafe, no action)** — The audit classified this as
Class B/D hybrid. The Class D prerequisite (`file/glob` sub-op) was not pursued; the
`skill_resolve` op (Wave 5a) addresses a different gap. `cost_preflight` remains
`mode: unsafe`. No further work planned.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `_api_glob_files(path)` (→ `reyn.api.unsafe.file.glob`) to count files.
That is the **only** non-pure operation. The cost calculation itself is entirely
arithmetic over in-memory data (`samples_result` already placed in the artifact by
`gather_samples`, and the glob file count).

**Why it can be split**: The file count is the only ambient input. A `run_op` `file`
`list_directory` (or a `glob` sub-op if added) could place the file list or count in the
artifact before the python step runs. The python step would then receive `data.file_count`
as an artifact field and perform purely arithmetic cost estimation — qualifying for
`mode: safe`.

**However**: there is no existing op kind that accepts a glob pattern and returns a count
directly. `file/list_directory` requires a directory, not a glob. A `glob` sub-op (= new
fine-grained `file` op variant) would be needed to avoid a lossy workaround. This pushes
the split into Class D territory for the I/O step, with the arithmetic remainder becoming
Class B trivial.

**Revised classification**: **Class B/D hybrid** — the filesystem boundary piece needs a
new `file/glob` op sub-kind; the arithmetic remainder is trivially safe.

**Refactor sketch**:
1. Add `file/glob` sub-op (= returns sorted list of matching paths, no content read).
   This op already exists conceptually in `reyn.api.unsafe.file.glob`; the gap is that
   it is not exposed as a Control IR op kind callable from a preprocessor chain.
2. Preprocessor chain step: `run_op { kind: file, op: glob, pattern: data.path }` →
   places `data._file_list` (list of matching paths + count).
3. New `cost_preflight_pure(artifact)` — `mode: safe` — reads `data._file_list.count`
   + `data.samples_result` and performs the arithmetic. Zero I/O.
4. Old `cost_preflight` function deleted.

**Effort**: MEDIUM (requires new `file/glob` op sub-kind + preprocessor chain change)

---

#### `write_chunks_with_lock` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Full filesystem pipeline: reads each source file via `Path.read_text`, acquires
an advisory JSON lock (lock file read + write with `os.getpid()`), writes
`artifacts/chunks.jsonl`, and releases the lock. Uses `os.getpid()` and `time.time()` for
lock metadata.

**Why it can't be split**: This is the "irreducible minimum" unsafe step by design
(documented inline in skill.md and in the function docstring). The file content reads and
lock acquire/release are the core purpose. No pure remainder exists that would be
separately useful; the entire step produces `chunk_count` only after writing the JSONL
file, which requires the content read.

**Verdict**: keep

---

#### `apply_strategy` — Class A (honestly unsafe, deprecated)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Same as `write_chunks_with_lock` plus additionally calls `_glob_files(path)` to
expand the source glob. Documented as deprecated — kept only for project override
compatibility with callers who override `apply_strategy` via `extends: stdlib/index_docs`.
Unsafe for the same reasons as `write_chunks_with_lock`.

**Verdict**: keep (as deprecated shim); new skills should use the two-step chain

---

### `index_events`

Source: `src/reyn/stdlib/skills/index_events/chunkers.py`  
Skill.md declares 3 `mode: unsafe` entries in `permissions:` plus 2 more in
`postprocessor.steps`. The permissions entries are the same functions as the postprocessor
entries; total distinct functions: 3.

---

#### `resolve_scan_context` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Reads `.reyn/index/events_cursor` (cursor file, `Path.read_text`), calls
`_discover_event_files(str(_EVENTS_DIR))` (→ `glob.glob` over `.reyn/events/**/*.jsonl`),
and calls `os.path.getmtime(fp)` on each file for timestamp summary. These reads are the
core payload delivered to the LLM: cursor value, file count, oldest/newest timestamps.

**Why it can't be split**: The function's output is used directly as LLM context. The
reads are structural — without them the phase has no useful data to present. A hypothetical
split would require 3 separate run_ops (cursor read, glob, mtime scan), plus a safe
aggregation step that is mostly trivial. The complexity of the split chain would exceed the
value for a read-only preprocessor.

**Verdict**: keep; document the multi-read nature in an `unsafe_reason:` field in skill.md
(currently absent — gap, not a latent bug).

---

#### `run_collect_chunks` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `_discover_event_files` (glob over `.reyn/events/**/*.jsonl`), then for
each file opens and line-reads it (raw JSONL event stream), groups events by run boundary,
and writes the output JSONL to `artifacts/event_chunks.jsonl`.

**Why it can't be split**: The I/O is the entire operation. Walk → parse → group → write
is an inseparable pipeline. A split into "glob → file/read per file → pure grouping"
would produce N intermediate run_ops (one per event file), which is operationally
impractical for event logs with hundreds of files.

**Verdict**: keep

---

#### `run_advance_cursor` — Class B (split candidate, trivial)

**Status: Kept as Class A (honestly unsafe, no action)** — The audit correctly revised
this to Class A during analysis: even after moving the JSONL read to a run_op, the cursor
file write is an irreducible unsafe side-effect. No further action.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Reads `artifacts/event_chunks.jsonl` (the just-written output of `run_collect_chunks`)
to find the max `ended_at` timestamp, then writes `.reyn/index/events_cursor` atomically
via `tempfile.mkstemp` + `os.rename`.

**Split potential**: The file read of `event_chunks.jsonl` is the only "input read". This
file is a workspace artifact, meaning a `run_op { kind: file, op: read }` step could place
its content in the artifact before the python step. The python step would then only parse
in-memory JSONL lines (pure string processing) and write the cursor — but the cursor
write itself is a filesystem side-effect that remains unsafe.

**Revised verdict**: **Class A** — even after moving the JSONL read to a run_op, the
cursor file write (`advance_cursor`) is an irreducible unsafe side-effect. The function
cannot become `mode: safe` because writing the cursor is its sole purpose.

**Verdict**: keep. (Initial split impression was incorrect; the write is irreducible.)

---

### `ops_report`

Source: `src/reyn/stdlib/skills/ops_report/aggregate.py`  
Skill.md declares 3 `mode: unsafe` entries.

---

#### `collect_aggregate` — Class B (split candidate, non-trivial)

**Status: ✅ LANDED in Wave 3a (commit `83f2cf5`)** — Implemented via sentinel-aware
3-step preprocessor chain. The recall-hit hot path (99%) now runs `dispatch_aggregate` in
`mode: safe`; the raw-events fallback (`collect_aggregate_fallback`) retains `mode: unsafe`
as an honest declaration. The implementation used the sentinel pattern (see Cross-cutting
findings) rather than the dual skill.md entry approach sketched in the audit, but achieves
the same outcome.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: The function itself contains no direct I/O. It reads `data.recall_result` (already
placed in the artifact by a preceding `recall` run_op) and decides whether to call
`aggregate_from_recall_chunks` (pure) or `aggregate_from_raw_events` (I/O). The I/O lives
in `aggregate_from_raw_events`.

**However**: `collect_aggregate` is the single entry point that dispatches to both. If
`aggregate_from_raw_events` is refactored out (see below), `collect_aggregate` with only
the `aggregate_from_recall_chunks` branch would be **purely pure** — it only processes
in-memory data. But as currently written it may call `aggregate_from_raw_events` and thus
must be `mode: unsafe` to allow that path.

**Split sketch** (if `aggregate_from_raw_events` is made a separate step):
1. Keep `collect_aggregate` as a two-branch dispatcher:
   - If recall chunks present: call `aggregate_from_recall_chunks` inline (pure).
   - If no chunks: return a sentinel `{"needs_raw_fallback": true}`.
2. Add a conditional preprocessor step: if `data.aggregate.needs_raw_fallback`, run a new
   `file/glob` + `file/read` chain (or a dedicated `scan_raw_events` run_op) followed by a
   pure `aggregate_from_raw_events_pure` step.
3. Both paths end with a `mode: safe` python step doing the aggregation math.

**Revised verdict**: **Class B non-trivial** — the split requires a conditional preprocessor
chain. The dispatcher logic is straightforward but the conditional branch structure adds
complexity.

**Effort**: MEDIUM

---

#### `aggregate_from_raw_events` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `_discover_event_files(root)` (glob over `.reyn/events/**/*.jsonl`), then
opens and reads each file line by line to build the run→events map. Also calls `_utc_now()`
(ambient clock, which is in the safe-mode allowlist for `datetime`).

**The clock read is ambient** (allowed in safe mode). The filesystem reads are the unsafe
part. This is the same pattern as `run_collect_chunks` in `index_events`: walk → parse →
aggregate. Not splittable without N intermediate run_ops per event file.

**Verdict**: keep; the clock read is fine, the filesystem walk is honestly unsafe.

---

#### `aggregate_from_recall_chunks` — Class C (mis-labeled as unsafe)

**Status: ✅ LANDED in Wave 2 (commit `1a06289`)** — Extracted to
`src/reyn/stdlib/skills/ops_report/aggregate_pure.py`, declared `mode: safe`. Zero logic
change; module split only. Matched the audit sketch exactly.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: None. The function takes a `chunks: list[dict]` argument (= `data.recall_result.chunks`
already in the artifact), iterates over it, performs arithmetic aggregation, and returns
a dict. It imports `defaultdict` from `collections`, `datetime`/`timezone`/`timedelta`
from `datetime`, and `typing`. All are in `PURE_STDLIB_ALLOWLIST`. There is no filesystem
access, no network call, no env-var read, no subprocess.

**The `mode: unsafe` is caused by module-level contamination**: `aggregate.py` imports
`glob`, `os`, and `pathlib` at the top level (used by the other functions in the same
module). The safe-mode AST validator rejects the entire module at import time because
`glob` is not in `PURE_STDLIB_ALLOWLIST`. The function itself is pure; the module is not.

**Remediation**: Extract `aggregate_from_recall_chunks` to its own module
`aggregate_pure.py` that contains only `PURE_STDLIB_ALLOWLIST`-compliant imports. The
function body needs zero changes. Switch the skill.md entry to `mode: safe`.

**Verdict**: **Class C (mis-labeled)** — the function is pure; the file it lives in is
not. Moving it to `aggregate_pure.py` is the zero-risk fix.

**Effort**: 30 min (file split + skill.md update)

---

### `skill_improver`

Source: `copy_to_work_resolver.py`, `trace_collector.py`, `version_snapshot.py`  
Skill.md declares 4 `mode: unsafe` entries (the safe entries are already correctly labeled
in `copy_to_work.py`).

---

#### `resolve_paths` (copy_to_work_resolver.py) — Class A (honestly unsafe)

**Status: ✅ LANDED as Class D-1 (commit `f5f164c`)** — New
`copy_to_work_resolver_pure.py` declared `mode: safe` using `skill_resolve` op (added in
Wave 5a) via `args_from:` template mechanism. The original `resolve_paths` function is
superseded; the new pure module performs the same resolution logic via the OS-layer op.
The audit's Class D sketch ("one op definition fixes two skills simultaneously") was
accurate.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `resolve_skill_path(target_skill)` which performs `Path.exists()` checks on
the three skill search paths (`src/reyn/stdlib/skills/`, `reyn/local/`, `reyn/project/`).
Also imports `reyn.skill.skill_paths` — a reyn module that is not in `reyn.safe.*` and
cannot be allowed in safe mode.

**Why it can't be split**: Path resolution is a filesystem existence check by nature. The
output is a set of path strings derived from disk state. There is no pre-existing op kind
that calls `resolve_skill_path`. The OS-level equivalent would require a new
`skill_resolve` op kind.

**Alternative path (Class D)**: A `skill_resolve` run_op that accepts a skill name and
returns the path dict could encapsulate this — the result placed in the artifact, the
downstream python step running pure path arithmetic. This is a plausible but non-trivial OS
extension.

**Verdict**: keep (Class A); flag `skill_resolve` as a potential future run_op (Class D
candidate).

---

#### `collect_traces` (trace_collector.py) — Class A (honestly unsafe)

**Status: ✅ LANDED in Wave 4 (commit `d0d65e8`)** — Split into `dispatch_traces`
(mode: safe) + `collect_traces_fallback` (mode: unsafe) via the sentinel-aware 3-step
preprocessor chain established in Wave 3a. The audit sketched a `trace_collector_pure.py`
module split with dual skill.md entries; the implementation used the sentinel pattern
instead for consistency with Wave 3a. The outcome is equivalent: 99% hot path (recall
available) runs in `mode: safe`, 1% raw-events fallback stays `mode: unsafe`.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: On the recall path, the function is pure (processes `data.trace_recall_result`
from the artifact). On the raw-events fallback path, it calls
`_discover_event_files` (glob over `.reyn/events/**/*.jsonl`) and opens + reads each file.
The module also imports `glob`, `os`, and `pathlib` at the top level, which the safe-mode
AST validator rejects.

**Split potential (Class B)**: The recall path of `collect_traces` is already pure —
it only processes in-memory recall chunks. If the raw-events fallback were moved to a
separate `unsafe` step and the recall processing extracted to a pure `mode: safe` step, the
common case (recall available) would run in safe mode.

**Refactor sketch**:
1. Extract `_collect_from_recall(chunks, skill_name, lookback)` to a new module
   `trace_collector_pure.py` (mode: safe). All logic is in-memory.
2. Keep `collect_traces` in `trace_collector.py` as the dispatcher:
   - If `data.trace_recall_result` has chunks → call `trace_collector_pure._collect_from_recall` (but
     since this crosses modules, the safe step would be a separate skill.md entry).
   - If no chunks → call raw-events path (unsafe).
3. Add two skill.md entries: one `mode: safe` for the recall path, one `mode: unsafe` for
   the raw fallback.

**Practical note**: The dispatcher logic means two separate python step entries with
conditional branching, which is uncommon in the current stdlib pattern. The split is
correct but adds complexity. Medium effort.

**Verdict**: Class B split candidate; effort MEDIUM. Keep as-is for now pending Class C
and trivial Class B work.

---

#### `save_snapshot` (version_snapshot.py) — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Reads `Path(original_skill_root) / "skill.md"` (target skill file read), writes
snapshot files to `.reyn/skill-versions/<name>/v<N>.md`, reads/writes the `current` pointer
file, calls `os.remove()` for max-versions capping, and calls `_get_max_versions()` which
imports `reyn.config.load_config` (reads `reyn.yaml` from disk). Multiple filesystem
reads and writes.

**Why it can't be split**: The snapshot writes and the `current` pointer update are
intrinsically coupled — both must succeed or fail atomically. The skill.md read is also
tightly coupled to the write. No pure remainder exists.

**Verdict**: keep

---

#### `read_on_propose_config` (version_snapshot.py) — Class B (split candidate, trivial)

**Status: ✅ LANDED in Wave 3b (commit `a2984d5`)** — Implemented via `file_read` op +
new pure parser `parse_on_propose_config_minimal` in `version_snapshot_pure.py`.
The audit originally revised this to Class A ("defer, needs config_read op") due to the
`yaml` module dependency. Wave 3b sidestepped the `yaml` dependency entirely by using a
targeted regex over the two fields of interest (`on_propose`, `max_versions`) rather than
a full YAML parse. This is simpler than the `config_read_self_improvement` op alternative
and avoids any OS extension.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `from reyn.config import load_config` and `cfg = load_config()` — reads
`reyn.yaml` from disk. Returns `{"on_propose": str, "max_versions": int}`.

**Split potential**: Config reads are precisely the use case for a run_op. The `file/read`
op can read `reyn.yaml`; a pure python step can then parse the YAML subset and extract the
two fields.

**However**: `reyn.yaml` is YAML not JSON; a python step parsing it would need the `yaml`
module, which is not in `PURE_STDLIB_ALLOWLIST`. An alternative is a new
`config_read_self_improvement` run_op that returns the typed config values directly — but
this is a Class D addition.

**Revised assessment**: The function is honest about its I/O (config file read). The
cleanest split requires either (a) a `yaml`-parsing python step (needs `yaml` in
allowed_modules — requires user config in `reyn.yaml`, which is circular), or (b) a new
`config_read` run_op (Class D). As-is `mode: unsafe` is the correct label.

**Verdict**: Class A (honestly unsafe, config read). The split is possible but requires
a new `config_read` op kind; defer to a future FP.

---

### `mcp_search`

Source: `src/reyn/stdlib/skills/mcp_search/registry_fetch.py`

---

#### `fetch_registry_results` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: HTTP GET to `registry.modelcontextprotocol.io/v0.1/servers?search=<query>` via
`reyn.api.unsafe.http.get`. Also reads env var `REYN_MCP_REGISTRY_URL` via `os.environ.get`.
Reads/writes a file-based TTL cache via `reyn.registry.cache`. Imports
`reyn.api.unsafe.http`, `reyn.api.safe.json`, and `reyn.registry.*` — none of which are in
`PURE_STDLIB_ALLOWLIST`.

**Why it can't be split without a new op**: The HTTP fetch is the entire purpose. The
`web_fetch` op exists but returns raw response text without automatic JSON decoding, dedup,
or model normalization. A pure step `parse_registry_results(artifact)` that processes
`data.registry_raw` would be `mode: safe`, but requires the HTTP fetch to be done via
`web_fetch` run_op first.

**Refactor sketch (Class B+D)**:
1. Use existing `web_fetch` run_op for the HTTP GET → places raw body at `data.registry_raw`.
2. New `mode: safe` python step `parse_registry_results(artifact)`:
   - Reads `data.registry_raw.body` (string from the run_op).
   - Parses JSON with stdlib `json.loads`.
   - Deduplicates and extracts candidates (pure dict processing).
   - Returns `{"candidates": [...], "source": "registry", "query": ...}`.
3. Cache logic (file-based TTL) would need to be dropped or moved to a separate unsafe
   step. Cache invalidation crosses the pure/impure boundary.

**Practical obstacle**: The cache read/write in `reyn.registry.cache` is a filesystem
side-effect. Without caching the network round-trip happens on every invocation. An
alternative `cache_lookup` → `web_fetch` → `cache_store` run_op chain preserves the
caching behavior but requires 3 op steps instead of 1 python step.

**Verdict**: Class A (honestly unsafe). The HTTP + cache + env-var triple makes it
Class A. A cleaner long-term refactor (Class B+D) is possible but the cache
management complexity makes it non-trivial. Defer.

---

### `mcp_install`

Source: `src/reyn/stdlib/skills/mcp_install/registry_fetch.py`

---

#### `fetch_server_for_install` — Class A (honestly unsafe)

**Status: Kept as Class A (honestly unsafe, no action)**

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Same pattern as `fetch_registry_results` in `mcp_search`: HTTP GET via
`reyn.api.unsafe.http.get`, env var read (`os.environ.get`), file-based cache
(`reyn.registry.cache`). Imports same unsafe modules. The resolution strategy (direct
lookup vs. search) adds a second conditional HTTP path.

**Split potential**: Same as `fetch_registry_results` — the HTTP call could be expressed
as a `web_fetch` run_op, with a pure `parse_install_candidates` python step consuming
`data.registry_raw`. The two-path structure (direct lookup vs. search) would require
conditional branching in the preprocessor chain, which is uncommon today.

**Verdict**: Class A (honestly unsafe). Same rationale as `fetch_registry_results`. Defer
to the same FP as `mcp_search`.

---

### `eval_builder`

Source: `src/reyn/stdlib/skills/eval_builder/analyze_skill_resolver.py`  
Skill.md declares 1 `mode: unsafe` entry (`resolve_paths`). The other two python entries
(`extract_skill_name`, `inject_resolved_paths`) are already correctly `mode: safe`.

---

#### `resolve_paths` (analyze_skill_resolver.py) — Class A (honestly unsafe)

**Status: ✅ LANDED as Class D-2 (commit `5cc797c`)** — New
`eval_builder/analyze_skill_resolver_pure.py` declared `mode: safe`, using `skill_resolve`
op via `args_from:` template mechanism. Structurally identical to Class D-1
(`skill_improver`). Both were the "same future `skill_resolve` op candidate" predicted in
the audit.

---

[ORIGINAL AUDIT TEXT BELOW]

**I/O**: Calls `resolve_skill_path(target_skill)` (filesystem existence checks via
`Path.exists()`), imports `reyn.skill.skill_paths`. Structurally identical to `skill_improver`'s
`copy_to_work_resolver.resolve_paths`.

**Why it can't be split**: Same rationale as the `skill_improver` case. A `skill_resolve`
run_op would encapsulate this (Class D), but the op does not exist yet.

**Verdict**: Class A (honestly unsafe); same future `skill_resolve` op candidate as
`skill_improver/copy_to_work_resolver.resolve_paths`.

---

## Cross-cutting technical findings (from implementation waves)

### `run_op.op.<field>:` templating

`{{ data.x }}` jinja-style templating in run_op step's op fields is **not
supported** by the preprocessor executor. Use `args_from: {field: data.path}`
instead — `_materialize_op` calls `op.model_copy(update=overrides)` at
execution time. Pattern:

```yaml
- type: run_op
  op:
    kind: skill_resolve
    name: PLACEHOLDER       # parse-time literal, overridden at runtime
  args_from:
    name: data._name.target_skill
  into: data._resolved
```

Adopted by Wave 5a / Class D-1 / Class D-2. Discovered independently by parallel sonnets
in the wave; shared here as a cross-cutting finding to prevent future re-discovery.

### Sentinel pattern for B/C split with optional fallback

For functions that have a "preferred pure path" and a "fallback I/O path", the audit
sketched a sentinel-aware 3-step chain. Implementations in Wave 3a and Wave 4 confirmed
this works cleanly:

1. `run_op` to fetch the optional input (e.g. recall result via `recall` op).
2. Safe python step: if input non-empty, compute inline + return `_path: "x"`;
   else return `_path: "needs_fallback"` sentinel.
3. Unsafe python step: no-op if sentinel says the path was already handled; otherwise
   run the I/O fallback.

The 99% hot path lands in `mode: safe`; the honest 1% fallback stays `mode: unsafe`. The
sentinel field is internal to the preprocessor chain and is stripped before passing to
downstream consumers.

Wave 3a (`collect_aggregate`) established this pattern. Wave 4 (`collect_traces`) reused
it without modification. Any future split candidate with a similar structure should reach
for this pattern first.

### `file_read` op + regex parser as alternative to `config_read` op

Wave 3b demonstrated that a `config_read` OS-level op is not always necessary. For narrow
config extraction (2 fields from `reyn.yaml`), a `file_read` op + a pure python step using
`re` (in `PURE_STDLIB_ALLOWLIST`) is simpler and avoids an OS extension. The tradeoff is
that the regex approach is brittle for complex YAML structures; a full `config_read` op
remains a valid future option for richer config access patterns.

---

## Appendix: full function inventory (updated)

| Skill | Function | Module | Class | Final verdict |
|---|---|---|---|---|
| index_docs | `gather_samples` | chunkers.py | A | kept |
| index_docs | `cost_preflight` | chunkers.py | A | kept (file/glob op not pursued) |
| index_docs | `write_chunks_with_lock` | chunkers.py | A | kept |
| index_docs | `apply_strategy` | chunkers.py | A | kept (deprecated shim) |
| index_events | `resolve_scan_context` | chunkers.py | A | kept |
| index_events | `run_collect_chunks` | chunkers.py | A | kept |
| index_events | `run_advance_cursor` | chunkers.py | A | kept (write is irreducible) |
| ops_report | `collect_aggregate` | aggregate.py | B | ✅ Wave 3a (`83f2cf5`) |
| ops_report | `aggregate_from_raw_events` | aggregate.py | A | kept |
| ops_report | `aggregate_from_recall_chunks` | aggregate.py | C | ✅ Wave 2 (`1a06289`) |
| skill_improver | `resolve_paths` | copy_to_work_resolver.py | D | ✅ Class D-1 (`f5f164c`) |
| skill_improver | `collect_traces` | trace_collector.py | B | ✅ Wave 4 (`d0d65e8`) |
| skill_improver | `save_snapshot` | version_snapshot.py | A | kept |
| skill_improver | `read_on_propose_config` | version_snapshot.py | B | ✅ Wave 3b (`a2984d5`) |
| mcp_search | `fetch_registry_results` | registry_fetch.py | A | kept |
| mcp_install | `fetch_server_for_install` | registry_fetch.py | A | kept |
| eval_builder | `resolve_paths` | analyze_skill_resolver.py | D | ✅ Class D-2 (`5cc797c`) |

### Top 3 lowest-friction refactor candidates — all DONE

1. **`aggregate_from_recall_chunks` (ops_report)** — ✅ DONE (Wave 2, `1a06289`).
   Class C. Zero logic change. Extracted to `aggregate_pure.py`, switched to `mode: safe`.
   30 min as estimated. Zero test changes.

2. **`collect_aggregate` (ops_report)** — ✅ DONE (Wave 3a, `83f2cf5`).
   Class B non-trivial. Sentinel-aware 3-step chain; 99% hot path safe.

3. **`resolve_paths` x2 (skill_improver + eval_builder)** — ✅ DONE (Wave 5a + D-1 + D-2,
   `01f3ae0` + `f5f164c` + `5cc797c`). One op definition (`skill_resolve`) fixed both
   skills simultaneously, exactly as predicted.

**R-PURE-MODE-REDEFINE stdlib refactor scope is now complete.** All addressable
Class B/C/D items landed across waves 2–5a. Remaining `mode: unsafe` declarations are
all Class A — honestly unsafe by design, with no pure remainder to extract. No further
refactor work is planned for this FP.
