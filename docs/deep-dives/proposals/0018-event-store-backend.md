# FP-0018: Event Store Backend Abstraction — JSONL / SQLite / DuckDB

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Priority**: LOW

---

## Summary

The current `EventStore` (`src/reyn/events/event_store.py`) writes events as append-only JSONL
files with rotation. This is correct and sufficient for current scale (dozens–hundreds of events
per session). As Reyn grows toward OSS adoption and higher-volume workloads (FP-0007 eval export,
FP-0012 async long-running skills), the JSONL-only implementation will become a performance
bottleneck on the read path. This proposal introduces an `EventStoreBackend` Protocol — the
same backend-abstraction pattern established by FP-0017 — plus three concrete backends: the
existing JSONL logic refactored as `JSONLBackend` (default, no migration), `SQLiteBackend` for
indexed resume reads, and `DuckDBBackend` for analytical eval-export workloads.

---

## Motivation

### Current implementation — performance profile

**Write path** (`write()`):
- Opens, appends, and closes the file synchronously on every call — no buffering, no batching.
- Calls `stat()` on every write to check the rotation threshold.
- Acceptable for current scale; each session produces at most a few hundred events.

**Read path** (`iter_all()`):
- Full sequential scan of all JSONL files on every call.
- No index: finding events for a specific `run_id` or `event_type` requires reading every line.
- Current callers: skill resume (WAL replay), FP-0007 eval export, `skill_resume_analyzer`.

### Why JSONL alone does not scale for indexed reads

DuckDB on JSONL delivers faster scans than Python's sequential loop (vectorized execution,
multi-file parallelism, column projection), but **still performs a full scan** — it cannot skip
to a position without reading from the beginning. For "find all events for `run_id` X", neither
JSONL+Python nor JSONL+DuckDB avoids O(n) work.

True O(log n) reads require a proper index:

- **SQLite + index** on `(run_id, timestamp, event_type)` — point lookups become B-tree
  traversals.
- **DuckDB native format or Parquet** — columnar min/max statistics enable chunk skipping; still
  not O(log n) for `run_id` point lookups, but orders of magnitude faster for analytical
  aggregations across all events.

### Use-case mapping

| Use case | Current bottleneck | Best backend |
|---|---|---|
| Resume (find events for run_id) | Full sequential scan | SQLite (indexed) |
| Eval export (aggregate all events of a type) | Full sequential scan | DuckDB or SQLite |
| Audit trail (append-only, human-readable) | open/close per write | JSONL (keep as-is) |
| Cost tab aggregation | Already in-memory | No change needed |

### Why not just switch to SQLite today

JSONL has real advantages worth preserving:

- **Human-readable**: `tail -f events/.../*.jsonl` is the fastest debugging tool in the current
  workflow.
- **Crash-safe without transactions**: a partial write leaves the last line truncated;
  `iter_all()` already skips partial lines. SQLite WAL provides equivalent safety but is less
  transparent.
- **Zero dependencies**: no new packages required.
- **Simplicity**: the current 160-line implementation is auditable in minutes.

The abstraction layer preserves the option to switch backends without committing to a migration
today. JSONL remains the default; SQLite or DuckDB are opt-in via `reyn.yaml`.

### Forward-looking pressure

- **FP-0007** (eval export) will aggregate large event histories across multiple runs for
  regression analysis. Full-scan cost grows linearly with history depth.
- **FP-0012** (async long-running skills) will generate high-frequency events over hours,
  increasing file count and total bytes scanned per resume operation.
- **OSS adoption** will surface users with event histories orders of magnitude larger than the
  current dogfood environment.

### Design inspiration — same pattern as FP-0017

FP-0017 established the `SandboxBackend` Protocol for execution isolation: skills declare policy,
the OS selects the enforcement backend. FP-0018 applies the identical pattern to event storage:
callers write and read events through a uniform API; the OS selects the storage backend from
`reyn.yaml`. Skill code and OS phase-execution code never reference backend types.

---

## Proposed implementation

### Abstraction layers

```
EventStoreBackend (how events are stored)   ← selected by OS from reyn.yaml
    ↓
EventFilter (what to read)                  ← passed by OS callers; never by LLM
```

### Backend Protocol and EventFilter

**`src/reyn/events/backend.py`**:

```python
class EventStoreBackend(Protocol):
    def write(self, event: Event) -> None: ...
    def iter_events(self, filter: EventFilter | None = None) -> Iterator[Event]: ...
    def iter_files(self) -> list[Path]: ...  # backward compat for existing callers
    def close(self) -> None: ...
```

**`src/reyn/events/filter.py`**:

```python
@dataclass
class EventFilter:
    run_id: str | None = None
    event_types: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
```

`iter_files()` is retained for backward compatibility with callers that inspect file paths
directly (e.g., the `reyn events` CLI subcommand). All new callers should use `iter_events()`.

### Component A — Protocol + JSONLBackend refactor (SMALL)

Define `EventStoreBackend` Protocol and `EventFilter`. Refactor the current `EventStore` into
`JSONLBackend` implementing the Protocol. The public `EventStore` class becomes a thin wrapper
that instantiates the configured backend — all existing callers are unchanged.

`JSONLBackend` behaviour is identical to the current `EventStore`: file rotation, chronological
ordering, partial-line skip on bad lines. No behaviour change, no migration needed.

**Target files**:
- `src/reyn/events/backend.py` — `EventStoreBackend` Protocol
- `src/reyn/events/filter.py` — `EventFilter` dataclass
- `src/reyn/events/backends/jsonl.py` — `JSONLBackend` (extracted from current `EventStore`)
- `src/reyn/events/event_store.py` — refactored to delegate to backend

### Component B — SQLiteBackend (SMALL)

`sqlite3` stdlib only; no new dependencies.

```python
class SQLiteBackend(EventStoreBackend):
    # Schema: events(id INTEGER PRIMARY KEY, run_id TEXT, event_type TEXT,
    #                 timestamp TEXT, payload TEXT)
    # Indexes: (run_id), (event_type), (timestamp)
    # Write buffering: configurable flush_interval_seconds (default: 1.0)
    ...
```

Write buffering reduces open/close overhead: events are accumulated in memory and flushed to
SQLite on a configurable interval (default 1 second) or on `close()`. The flush is a single
`executemany()` wrapped in a transaction — far cheaper than one file-open per event.

`iter_events(filter)` translates `EventFilter` to a parameterized SQL `WHERE` clause. A
`run_id` point lookup becomes a B-tree index scan: O(log n + k) where k is the result count.

`iter_files()` returns the SQLite database path as a single-element list for backward
compatibility.

**Target files**:
- `src/reyn/events/backends/sqlite.py` — `SQLiteBackend`

### Component C — DuckDBBackend (MEDIUM)

Requires the `duckdb` PyPI package (extra dependency, opt-in only).

```python
class DuckDBBackend(EventStoreBackend):
    # Primary write target: DuckDB native format
    # Also queryable against existing JSONL via read_json_auto —
    # useful for migrating existing sessions without copying data.
    ...
```

`DuckDBBackend` is the best fit for FP-0007 eval-export workloads: columnar storage + vectorized
execution makes `GROUP BY event_type` / `WHERE timestamp BETWEEN ...` queries orders of magnitude
faster than JSONL+Python at scale. It can also query existing JSONL files via
`read_json_auto('<dir>/**/*.jsonl')` without migrating data, which preserves the human-readable
audit trail while enabling analytical queries.

**Target files**:
- `src/reyn/events/backends/duckdb.py` — `DuckDBBackend`

### Component D — Auto-selection + reyn.yaml config (SMALL)

**`reyn.yaml`**:

```yaml
events:
  backend: jsonl    # jsonl | sqlite | duckdb (default: jsonl)
  sqlite:
    flush_interval_seconds: 1.0   # write buffer flush interval
  duckdb:
    also_query_jsonl: false       # set true to query legacy JSONL alongside DuckDB files
```

Auto-selection logic in `src/reyn/events/event_store.py`: reads `events.backend` from
`ReynConfig`, instantiates the corresponding backend, raises `ConfigError` with a clear message
if `duckdb` is selected but the package is not installed.

**Target files**:
- `src/reyn/events/event_store.py` — backend factory + config wiring
- `src/reyn/config.py` — `EventsConfig` dataclass (`backend`, `sqlite`, `duckdb` sub-configs)

---

## Priority ordering

**A → D → B → C**

Component A (Protocol definition) can land at any time at SMALL cost — it is a pure refactor
with no behaviour change and is the foundation everything else builds on. Component D (config
wiring) comes next to make the abstraction configurable. Components B and C are deferred until a
concrete performance regression is observed in the field.

---

## Alignment with Reyn principles

| Principle | How this FP aligns |
|---|---|
| P3 | OS selects the backend from `reyn.yaml`; skills and the LLM never touch the storage layer. |
| P5 | Workspace path remains the root for JSONL files; SQLite and DuckDB databases also live under the workspace. All backends write to OS-managed paths. |
| P6 | The append-only semantic and event schema are unchanged across all backends. The audit trail guarantee (every state change emits an event) is a property of the `EventStore` caller, not the backend. |
| P7 | `EventStore` callers (OS phase execution, skill resume) never reference backend type names. Backend selection is a config-driven OS concern. |
| P8 | Phase instructions never describe storage layer choices; this is invisible to the LLM. |

---

## Dependencies

- **Component A**: none — pure internal refactor of `event_store.py`.
- **Component B**: none — `sqlite3` is stdlib.
- **Component C**: `duckdb` PyPI package. FP-0007 (eval export) is the primary consumer that
  would benefit from this backend. No hard ordering dependency — FP-0007 can proceed against
  the JSONL backend and migrate later.
- **Component D**: Component A must land first (backend Protocol must exist before the factory
  can instantiate it).

---

## Cost estimate

| Component | Cost | Notes |
|---|---|---|
| A: Protocol + JSONLBackend refactor | SMALL | Pure extract-and-rename; no behaviour change |
| B: `SQLiteBackend` | SMALL | `sqlite3` stdlib; index schema is straightforward |
| C: `DuckDBBackend` | MEDIUM | Extra dependency; `read_json_auto` bridge adds complexity |
| D: Config wiring + auto-selection | SMALL | Config dataclass + factory method |
| Tests | SMALL | Tier 1: `EventStoreBackend` contract (write + iter_events); Tier 2: backend auto-selection invariant |

**Total active work: MEDIUM** (but priority LOW — defer B/C until a concrete performance
regression is observed)

---

## Related

- `src/reyn/events/event_store.py` — current implementation (Components A, D: refactor)
- `src/reyn/events/backends/jsonl.py` — new file (Component A)
- `src/reyn/events/backends/sqlite.py` — new file (Component B)
- `src/reyn/events/backends/duckdb.py` — new file (Component C)
- `src/reyn/events/backend.py` — new file (Component A: Protocol)
- `src/reyn/events/filter.py` — new file (Component A: EventFilter)
- `src/reyn/config.py` — `EventsConfig` (Component D)
- FP-0007 (`0007-evaluation-infrastructure.md`) — primary consumer of Component C
- FP-0012 (`0012-async-skill-execution.md`) — high-frequency event source that will stress the
  write path; Component B write buffering directly addresses this
- FP-0017 (`0017-sandboxed-execution.md`) — established the `SandboxBackend` Protocol pattern
  that this FP follows
