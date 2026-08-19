# 0068 — `SecretProvider` seam: implementation plan

**Status**: proposal (2026-08-19). Decisions it implements: [ADR-0043](../decisions/0043-generic-secret-provider-seam.md) (**Proposed**, not accepted — this plan is contingent on that ADR being ratified).
**Review it came from**: #4890.

**One line:** add a credential lookup boundary that only *new* consumers use, wire the dormant `ScopedSecretStore` to it, and ship a value-free readiness probe before any native backend exists.

## 0. What this is not

It is not a migration. `os.environ` is read at 133 lines across 60 files under `src/reyn/` (upper bound — includes non-secret flags like `REYN_PROF_DUMP`; none were opened). ADR-0043 §2.2 decides those stay as they are. A reviewer who sees "provider" and expects existing call sites to change is reading the wrong plan.

It is also not an OAuth change. FP-0016 Component B's separation stands (ADR-0043 §2.4).

## 1. Phases

Each phase names what would make it *not* done, because "the class exists" has already proven insufficient once here — see Phase 2's own subject.

### Phase 1 — the seam, reachable by nobody yet

- `SecretReference` — a value type: `provider` (`env` | `file` | `os` | `auto`), `service`, `account | None`.
- `SecretProvider` — a `Protocol` with a single lookup returning a handle or metadata, never a bare value into caller scope.
- `EnvSecretProvider`, `FileSecretProvider` — adapters over the two sources that exist today. They read the same places `loader.py` already reads; they do not change what `loader.py` does at startup.
- `SecretResolver` — picks a provider for a reference; `auto` order is explicit and refuses ambiguity rather than guessing.

**Not done if**: the resolver can return a value that a tool result or audit payload could serialise; `auto` silently picks when two providers both match; any existing component's behaviour changed.

### Phase 2 — wire the dormant capability gate

`ScopedSecretStore` is constructed 0 times under `src/reyn/` today and 8 times in `tests/core/test_fp0016_d_e2e_confused_deputy.py`; `OpContext.secret_store` (`context.py:273`) is a declaration with no assignment and no reader. Phase 2 connects it at the resolver so a reference outside `allowed_keys` raises `CredentialScopeError` on a real path.

**Not done if**: production still constructs it zero times; or the deny path is only reachable from tests. The falsification is direct — remove the wiring and a production-shaped denial must stop happening.

**Before writing code**: establish *why* it was never wired (ADR-0043 §6). A dropped remainder and a deliberate block look identical from the call-site count alone, and only one of them is safe to simply finish.

### Phase 3 — readiness, without values

`reyn doctor` reports, per declared reference: which provider would serve it, whether the store is reachable, whether the reference resolves. It prints no secret value and no evidence of one.

Reports use ADR-0043 §2.7's five states — `declared` / `resolved` / `injected` / `used` / `unknown` — and `unknown` is printed as `unknown`, not omitted and not rendered as a failure.

**Not done if**: a probe's output lets a reader distinguish two different secret values; or an unmeasured step renders as anything other than `unknown`.

### Phase 4 — one native backend, when a consumer names it

`keyring` is the candidate (it fronts macOS Keychain, Linux backends, and the Windows store). Do not add all three platforms; add the one a named consumer needs, behind an optional dependency.

**Not done if**: it ships without a named consumer; or an unlocked-store assumption is made from a successful import (importability is not availability, and the probe must distinguish them).

### Phase 5 — bindings and scoped injection (separate security review)

Operator-authored binding of a reference to a consumer, with narrowly scoped injection. This phase, not Phase 1, is where agent exposure is decided. It should not begin while Phase 2's question (why the gate was dormant) is unanswered.

## 2. Surfaces

`reyn credential add <name> --provider … --service … --account …` stores reference metadata only, never a value. `reyn credential verify <name>` resolves without printing. `reyn secret set` is unchanged and remains the value-entry surface for the file provider (ADR-0030's string, untouched — see ADR-0043 §5).

## 3. Risks this plan carries deliberately

- **Two lookup paths coexist.** Accepted in ADR-0043 §3, recorded here so the plan does not read as if uniformity were coming later by default.
- **Env injection is not a boundary.** Where the seam offers env injection it must say so at the point of offering, not in a distant document.
- **`auto` is the sharp edge.** Ambiguity handling is unspecified in ADR-0043 §6 and must be settled before `auto` ships, not after.

## 4. Open — carried from ADR-0043 §6

`auto` precedence; opaque-handle-only or not; extras vs plugins for native backends; how approval is persisted; and why `ScopedSecretStore` was never wired.

## 5. Measurement limits in this document

- The 133/60 figure is an upper bound; no line was opened, and non-secret `os.environ` reads are included.
- `ScopedSecretStore`'s dormancy is established by construction-site and name-occurrence counts, not by running anything.
- No claim here is based on executing Reyn; the author has no environment in which to exercise a credential store.
