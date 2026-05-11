---
type: concept
topic: architecture
audience: [human, agent]
---

# Python `pure` mode — "ambient sources only"

A python step in `mode: pure` is a sandboxed Python function call used by the
[preprocessor](preprocessor.md) and [postprocessor](postprocessor.md).
This page documents the property authors can rely on when deciding whether
to put a step in `pure` mode, and which stdlib modules are safe to import.

## The single property

> **`mode: pure`**: A python step's output is determined ONLY by its input
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

Everything else is **non-ambient** and stays out of `pure` mode:

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

## Currently-allowed stdlib modules

| Module | Ambient class |
|--------|---------------|
| `math`, `cmath`, `statistics`, `decimal`, `fractions`, `numbers` | pure compute |
| `string`, `re`, `textwrap`, `unicodedata` | pure compute |
| `json`, `base64`, `binascii`, `hashlib`, `hmac` | pure compute |
| `collections`, `itertools`, `functools`, `operator`, `copy` | pure compute |
| `enum`, `dataclasses`, `typing`, `abc` | pure compute |
| `random` | entropy |
| `secrets` | entropy |
| `time` | clock |
| `datetime`, `calendar` | clock |
| `zoneinfo` | bundled static data (tz database) |

The list of record is
[`src/reyn/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/kernel/_python_allowlist.py).
A project may extend it via `permissions.python.allowed_modules` in
`reyn.yaml`; the same "ambient sources only" property is the bar for any
extension.

## How to extend — when `pure` is not enough

If your step needs a capability that is not ambient — reading a file the
operator chose, calling an HTTP service, spawning a process, or reaching
into `os.environ` — do **not** request a new entry in the allowlist.
Instead, use `type: run_op` in the preprocessor / postprocessor chain.

`run_op` is the proper escape hatch:

- it goes through the OS op runtime, with its own permission gate;
- its capabilities are explicit (e.g. `read_file`, `http_request`) rather
  than implicit-via-import;
- it leaves an event log entry per call, so the audit story for
  non-ambient access stays intact ([P6](principles.md#p6-events-are-the-audit-truth)).

In short: **`pure` python is for deterministic-ish computation over inputs +
ambient sources. Everything else is a `run_op`.**

## See also

- [Reference: preprocessor DSL](../reference/dsl/preprocessor.md) — declaring `python` steps
- [Reference: postprocessor DSL](../reference/dsl/postprocessor.md) — same DSL on the finish side
- [Concept: preprocessor](preprocessor.md) — the deterministic-split story
- [Concept: postprocessor](postprocessor.md) — finish-side mirror
- [`src/reyn/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/kernel/_python_allowlist.py) — list of record
