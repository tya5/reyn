---
type: concept
topic: architecture
audience: [human, agent]
---

# Python `safe` mode — "ambient sources only"

A python step in `mode: safe` is a sandboxed Python function call used by the
[preprocessor](../skills/preprocessor.md) and [postprocessor](../skills/postprocessor.md).
This page documents the property authors can rely on when deciding whether
to put a step in `safe` mode, and which stdlib modules are safe to import.

> **Rename note**: `safe` was previously called `pure` (renamed in FP-0014).
> The companion mode `unsafe` was previously called `trusted`.

## Formal property: ambient sources only

A `mode: safe` python step's output is determined entirely by:

1. The input artifact (= explicit `args_from` dependencies)
2. **Ambient sources**, defined as:
   - **Clock**: `time`, `datetime.now()` — system wall clock + monotonic time
   - **Entropy**: `random`, `secrets` — `/dev/urandom`-backed PRNG / CSPRNG
   - **Bundled static data**: `zoneinfo` — IANA TZ database shipped with Python

Filesystem, network, subprocess, and environment access are syntactically
unreachable from a `mode: safe` step. The allowlist enforces this at
import time.

## Why "ambient" instead of "pure"

A literal "pure function" interpretation would exclude `time.time()` and
`random.random()` because both depend on hidden global state. But excluding
them would force every `mode: safe` step to receive clock/entropy as an
explicit input artifact — impractical, and not what authors expect.

The "ambient sources" framing acknowledges that some non-determinism is
acceptable as long as the source is well-defined and the value is not
under operator/attacker control.

## The single property

> **`mode: safe`**: A python step's output is determined ONLY by its input
> artifacts plus **ambient sources** — the wall clock, an entropy stream,
> and bundled stdlib static data. Filesystem, network, process, and
> environment access are syntactically unreachable.

This is the property the AST validator and the subprocess sandbox jointly
enforce. If you want to know whether a new stdlib module would be safe in
the allowlist, ask: *can every public call be satisfied from {inputs, clock,
entropy, bundled static data}?* If yes, it is ambient. If it needs anything
that the operator could change without redeploying Python — files,
environment variables, the network — it is not.

## Ambient vs non-ambient at a glance

| Class | What it means | Allowed examples |
|-------|---------------|------------------|
| **Inputs** | The artifact passed into the step | the `artifact` parameter |
| **Clock** | Current time as seen by the OS | `time.time()`, `datetime.now()` |
| **Entropy** | OS-provided randomness | `random`, `secrets` |
| **Bundled static data** | Files shipped with the Python install | `zoneinfo` (tz database) |

Everything else is **non-ambient** and stays out of `safe` mode:

| Non-ambient class | Why it is excluded | Typical modules |
|-------------------|--------------------|-----------------|
| Filesystem ingress | Reads operator-controlled state | `pathlib`, `glob`, `os.path`, `open` |
| Filesystem egress | Mutates operator-visible state | `open(..., "w")`, `shutil` |
| Network | External, unbounded, latency-bearing | `urllib`, `requests`, `socket`, `http` |
| Process control | Side effects outside the sandbox | `subprocess`, `os.system`, `os.fork` |
| Environment | Operator-tunable input that the step does not declare | `os.environ`, `os.getenv` |
| Dynamic code | Bypasses every other check | `eval`, `exec`, `compile`, `__import__` |

## Why some seemingly-I/O modules are allowed

A few entries in the allowlist look like I/O at a glance. They are kept
because their I/O is **ambient** — bundled with the Python install or
served from a managed kernel facility — not operator-tunable workspace
state:

- **`zoneinfo`** reads timezone files, but those files are shipped with
  Python (or the host's `tzdata` package). Given the same Python install,
  the answer is deterministic. The step cannot observe operator-edited
  files this way.
- **`random`** and **`secrets`** pull from the OS entropy stream. That is
  non-deterministic, but it is ambient — the step cannot use it to read
  workspace state, only to produce fresh bits.
- **`time`** / **`datetime.now()`** read the wall clock. Non-deterministic,
  but ambient and side-effect-free.
- **`hashlib`** / **`hmac`** are pure compute over their arguments.

The shared property: each of these is satisfiable from
*{inputs, clock, entropy, bundled static data}* alone. None of them lets
the step learn anything about the operator's filesystem, network, or
environment that the step did not already receive as input.

## Reading the allowlist

Each entry in [`src/reyn/core/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/core/kernel/_python_allowlist.py)
carries a short inline comment explaining why it satisfies the contract.
The categories are:

- `# ambient: ...` — falls under the formal property above (clock / entropy /
  bundled static data)
- `# restricted to ...` — admits the module but only pure operations
  (e.g. `pathlib.PurePath`, not `Path.read_text()`)
- `# pure` — no ambient access at all (e.g. `math`, `re`)

## Currently-allowed stdlib modules

| Module | Ambient class | Rationale |
|--------|---------------|-----------|
| `math`, `cmath`, `statistics` | pure compute | Math functions over numeric inputs only |
| `decimal`, `fractions`, `numbers` | pure compute | Arbitrary-precision and rational arithmetic; ABC only |
| `string`, `re`, `textwrap` | pure compute | String constants, regex, text wrapping — no I/O |
| `unicodedata` | pure compute | Unicode property tables compiled into CPython; no file I/O at runtime |
| `json`, `base64`, `binascii` | pure compute | Serialisation / codec over byte/string inputs |
| `hashlib`, `hmac` | pure compute | Cryptographic hash computation over inputs |
| `collections`, `itertools`, `functools`, `operator`, `copy` | pure compute | Container types, iterator combinators, higher-order functions |
| `enum`, `dataclasses`, `typing`, `abc` | pure compute | Type infrastructure; no runtime state |
| `__future__` | pure: compiler directives | Compiler flags only (`annotations`, `division`); no runtime capability |
| `random` | ambient: entropy | `/dev/urandom`-seeded PRNG — entropy I/O, not operator-state |
| `secrets` | ambient: entropy | `/dev/urandom`-backed CSPRNG — entropy I/O, not operator-state |
| `time` | ambient: clock | System wall clock + monotonic clock |
| `datetime` | ambient: clock | `datetime.now()` reads the wall clock; date arithmetic is pure |
| `calendar` | pure compute | Calendar arithmetic; no system clock read at any call site |
| `zoneinfo` | ambient: bundled static data | IANA TZ database shipped with Python — same install = deterministic |

The list of record is
[`src/reyn/core/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/core/kernel/_python_allowlist.py).
A project may extend it via `python.allowed_modules` in
`reyn.yaml`; the same "ambient sources only" property is the bar for any
extension.

## Stdlib auto-allow contract

`reyn run` applies different auto-allow rules depending on whether the skill is
a stdlib skill or a user skill:

| Context | `mode: safe` | `mode: unsafe` |
|---------|-------------|----------------|
| **stdlib skill** via `reyn run` (non-interactive) | auto-allowed (no prompt) | auto-allowed (no prompt) |
| **user skill** (`reyn/project/`, `reyn/local/`) non-interactive | auto-allowed (no prompt) | requires `--allow-unsafe-python` or interactive approval |
| **user skill** interactive run | auto-allowed (no prompt) | startup approval prompt |

The non-interactive auto-allow for user-skill `mode: safe` was added to mirror
the same behavior already in place for eval/CI runs (see [permission model](../runtime/permission-model.md#python-permission-and-mode-safe-allowlist)).

## How to refactor an unsafe step to safe

If your python step reads a file or calls a service, extract the I/O into a
preceding `run_op` step. The python step then receives the result as a plain
input and becomes a pure function of that value.

**Before (unsafe — reads a file inside python):**

```yaml
preprocessor:
  - type: python
    mode: unsafe
    fn: |
      import pathlib
      text = pathlib.Path(artifact["config_path"]).read_text()
      return {"lines": text.splitlines()}
```

**After (safe — I/O in run_op, compute in python):**

```yaml
preprocessor:
  - type: run_op
    op: read_file
    args:
      path: "{{ artifact.config_path }}"
    output_key: config_text

  - type: python
    mode: safe
    args_from: [artifact, data.config_text]
    fn: |
      return {"lines": config_text.splitlines()}
```

The pattern: **split I/O from compute**. Put the I/O in a `run_op` (where it
gets its own permission gate and event log entry per [P6](../architecture/principles.md#p6-events-are-the-audit-truth));
put the computation in a `mode: safe` python step.

## How to extend — when `safe` is not enough

If your step needs a capability that is not ambient — reading a file the
operator chose, calling an HTTP service, spawning a process, or reaching
into `os.environ` — do **not** request a new entry in the allowlist.
Instead, use `type: run_op` in the preprocessor / postprocessor chain.

`run_op` is the proper escape hatch:

- it goes through the OS op runtime, with its own permission gate;
- its capabilities are explicit (e.g. `read_file`, `http_request`) rather
  than implicit-via-import;
- it leaves an event log entry per call, so the audit story for
  non-ambient access stays intact ([P6](../architecture/principles.md#p6-events-are-the-audit-truth)).

In short: **`safe` python is for deterministic-ish computation over inputs +
ambient sources. Everything else is a `run_op`.**

## Stdlib safe-only doctrine (FP-0042)

**Stdlib skills must not require unsafe-mode python.** A regular operator
who does not pass `--allow-unsafe-python` should be able to run any
stdlib skill end-to-end. Stdlib that demands unsafe contradicts the
user-trust model Reyn promises.

This is the operating rule established by
[FP-0042](../../deep-dives/proposals/0042-stdlib-safe-only-and-permission-gated-file-api.md)
and enforced by `tests/test_fp0042_stdlib_safe_only.py`:

- No file under `src/reyn/stdlib/` imports from `reyn.api.unsafe.*`.
  Use the `reyn.safe.*` permission-gated surface instead (= `file`,
  `process`, `mcp.registry`).
- No new stdlib `skill.md` declares `mode: unsafe` python steps. The
  default mode for new stdlib code is `mode: safe`.

### No grandfathered exemptions

After FP-0042, the `GRANDFATHERED_UNSAFE` set is
**empty** — stdlib has zero `mode: unsafe` python steps. The last
holdout (`index_docs.apply_strategy`, a deprecated monolithic compat
path) was retired in that phase; its surveyed project-override
contract migrated to the two-step
`extract_and_split` + `write_chunks_with_lock` chain.

The enforcement tests in `tests/test_fp0042_stdlib_safe_only.py` now
express two invariants:

- `test_stdlib_mode_unsafe_only_in_exemption_set` — fails on any
  unsafe declaration outside the (empty) exemption set.
- `test_stdlib_unsafe_surface_is_zero` — the positive form: fails fast
  if any unsafe step appears anywhere in stdlib.

Adding a new stdlib unsafe step requires deleting the second test (=
deliberate architectural decision that needs broad review, not a
CI-silent change).

## See also

- [Concept: permission model](../runtime/permission-model.md) — the broader `python.safe` / `python.unsafe` permission keys and the `mode: safe` auto-allow rules
- [Concept: care boundary](../architecture/care-boundary.md) — what Reyn cares about vs. observes only
- [Reference: preprocessor DSL](../../reference/dsl/preprocessor.md) — declaring `python` steps
- [Reference: postprocessor DSL](../../reference/dsl/postprocessor.md) — same DSL on the finish side
- [Concept: preprocessor](../skills/preprocessor.md) — the deterministic-split story
- [Concept: postprocessor](../skills/postprocessor.md) — finish-side mirror
- [`src/reyn/core/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/core/kernel/_python_allowlist.py) — list of record
- [FP-0042 proposal](../../deep-dives/proposals/0042-stdlib-safe-only-and-permission-gated-file-api.md) — stdlib safe-only + permission-gated `reyn.safe.file` API
- [`tests/test_fp0042_stdlib_safe_only.py`](https://github.com/tya5/reyn/blob/main/tests/test_fp0042_stdlib_safe_only.py) — CI enforcement for the doctrine above
