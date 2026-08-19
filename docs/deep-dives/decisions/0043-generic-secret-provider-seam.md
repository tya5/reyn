# ADR-0043: A generic `SecretProvider` seam — new consumers only, and the capability gate that already exists gets wired

**Status**: Proposed (2026-08-19) — owner interest relayed via `reyn-reviewer`; **not** confirmed by the owner directly to the author of this ADR. Do not read `Proposed` as `Accepted`.
**Track**: Architecture — credential lookup, extending ADR-0030 (universal secret handling) and FP-0016 (OAuth lifecycle + scoped store).
**Supersedes**: nothing. See §5.

---

## 1. Context

Reyn can read credentials from the shell environment and from `~/.reyn/secrets.env`. It cannot read a credential a developer has already stored in the host's native store (macOS Keychain, Linux Secret Service, Windows Credential Manager), so a workstation login that other tools reuse is invisible to Reyn.

A design document (`SECRET_PROVIDER_PROPOSAL.md`, authored outside version control in the `reyn-self` workspace) proposed a generic `SecretProvider` abstraction to close that gap. This ADR records the decisions taken after reviewing it against what is actually in the tree (review: #4890).

Three measurements shaped these decisions; each is stated with its own limit.

**M1 — the current model is eager, not lazy.** `src/reyn/security/secrets/loader.py`'s own docstring: *"Called once at Reyn process startup (from `config.load_config()`) so that all components can read secrets via `os.environ.get()` without any knowledge of the dotenv file."* Secrets are pushed into `os.environ` at startup; components read the process environment, not a lookup API.

**M2 — the scale of converting that model.** `os.environ` is read at **133 lines across 60 files** under `src/reyn/`. *Limit: none of the 133 were opened; the figure includes non-secret configuration flags (e.g. `REYN_PROF_DUMP`), so it is an upper bound on the conversion surface, not a count of credential reads.*

**M3 — the capability gate exists as code and is not connected.** `ScopedSecretStore` (`src/reyn/security/secrets/store.py:115`) implements `allowed_keys`, `is_unrestricted`, a `_check` that raises `CredentialScopeError`, and `list_visible_keys`. It is constructed **0 times** under `src/reyn/` and 8 times inside `tests/core/test_fp0016_d_e2e_confused_deputy.py`. `OpContext.secret_store` (`src/reyn/core/op_runtime/context.py:273`) is the single occurrence of the name in `src/`: a type declaration with no assignment and no reader. *Limit: why it was never wired is not established — a deliberate staging or a dropped remainder are both consistent with what was measured.*

## 2. Decision

1. **Introduce a `SecretProvider` / `SecretReference` / `SecretResolver` seam** as the single lookup boundary for credentials obtained by *new* consumers.

2. **Do not migrate the existing `os.environ` reads.** The seam is a branch, not a replacement: existing components keep reading the process environment exactly as ADR-0030 specified. M2 is why — a "wrap the current paths as providers" phase that actually re-routes call sites is a 133-site change and cannot also be the "no behaviour change" phase it is usually described as. A façade nobody calls and a migration of every caller are different projects; this ADR chooses neither by conflation and picks the branch explicitly.

3. **Wire `ScopedSecretStore` at the resolver, rather than designing a second capability gate.** Per M3 the class is dormant, so this is *connecting an existing dormant gate*, not *reusing a working one*. Any text that says the capability layer "already exists" is false in the sense that matters: nothing in production can be denied by it today.

4. **OAuth stays in its own lifecycle.** `src/reyn/security/secrets/oauth.py` records the reason: *"`secrets.env` is a flat dotenv text file used for static keys … OAuth tokens have multiple fields plus a refresh lifecycle, which fits a structured [store]."* That is a decision already taken under FP-0016 Component B, not an open question. Re-unifying the backends would require a superseding ADR and a migration story for expiry semantics; neither is proposed here.

5. **Native backends are demand-gated.** Ship the seam, the reference type, and a value-free readiness probe first. Add a native backend when a named consumer needs it — not as a completeness exercise across three platforms.

6. **No Git- or GitHub-specific code in Reyn core.** Git credential-helper compatibility is a validation target: either a generic reference satisfies a standard credential request or it does not. Either answer is information; neither justifies a Git subsystem.

7. **Reports distinguish five states, and "unknown" is one of them**: `declared` (a reference exists in configuration), `resolved` (a provider returned a usable handle internally), `injected` (a consumer accepted it at its transport seam), `used` (the consumer completed an authenticated operation), `unknown` (that step was not measured). A provider declaration, a name in documentation, and a peer's summary are none of the first four.

## 3. Consequences

**Desirable.** New consumers get one lookup boundary instead of each inventing its own environment read. The readiness probe becomes possible before any credential is injected anywhere, which is the cheapest half of the value. Wiring `ScopedSecretStore` turns a tested-but-inert deny path into a live one — the confused-deputy scenario its tests already describe becomes something production can actually refuse.

**Undesirable, and accepted.** Two lookup paths coexist indefinitely: the ADR-0030 environment path and the provider seam. That is a real cost — a reader must ask which path a given credential took, and the answer will not be uniform. The alternative (converting 133 sites) buys uniformity at a price this ADR judges higher than the confusion it prevents. If a later measurement shows the split causing incidents rather than merely inelegance, that is grounds for a superseding ADR, and this paragraph is the record that the split was chosen rather than overlooked.

**Also accepted.** Environment-variable injection is not a secrecy boundary — child inheritance, debuggers, and process inspection all remain. The seam must document that where it offers env injection, rather than implying that routing through a provider makes the value safe.

## 4. Alternatives considered

- **Convert every `os.environ` read to the provider (M2's 133 sites).** Rejected for this ADR: it is a large behaviour-affecting change to a path ADR-0030 accepted, and it delivers nothing a new-consumer branch does not, until a second backend actually exists.
- **Design a fresh capability gate alongside `ScopedSecretStore`.** Rejected: two gates over one resource is the failure this repo keeps re-encountering, and the dormant one already carries the semantics (`allowed_keys`, `CredentialScopeError`) plus tests describing the attack it prevents.
- **Extract the provider as a standalone OSS project.** Rejected for now: the parts worth reusing (binding, permission, audit, injection) are the parts entangled with Reyn's runtime, and platform access is already covered by existing libraries.
- **Ship all three native backends up front.** Rejected: each adds optional dependencies, packaging matrix, and CI surface, and none has a named consumer yet.

## 5. Relationship to existing records

- **ADR-0030** (universal secret handling — `${VAR}`, `~/.reyn/secrets.env`, `reyn secret set/list/clear/rotate`) is **extended, not superseded**. Every string it names keeps its meaning: `${VAR}` interpolation is untouched, `secrets.env` remains the portable file path, and `reyn secret` remains the value-entry surface. A credential-reference syntax, if one is ever added, takes a separate namespace (e.g. `${credential:…}`) so that no sentence in ADR-0030 becomes false.
- **FP-0016** (OAuth lifecycle, Component B; scoped store, Component D) is **extended, not superseded**. Decision 4 preserves Component B's separation; decision 3 finally connects Component D's class.
- `reyn credential add` is proposed as an **additional** command, not a rename of `reyn secret set`. Renaming a string an accepted ADR names would itself require a superseding ADR (`docs/deep-dives/decisions/README.md`).

## 6. Open questions this ADR does not decide

- Deterministic precedence for `provider: auto`, and the behaviour when two providers can both satisfy a reference.
- Whether resolved values are returned as opaque handles only, and what the trusted injection seams are.
- Whether native backends ship as optional extras or as plugins.
- How operator approval for a binding is persisted, and which operations may reference credentials at all.
- Why `ScopedSecretStore` was never wired (M3's limit) — worth establishing before assuming the wiring is simply missing rather than blocked.

## 7. References

- Review and measurements: #4890.
- Source design document: `SECRET_PROVIDER_PROPOSAL.md` (reyn-self workspace; **not under version control** — its contents are summarised here rather than linked).
- Implementation plan: `docs/deep-dives/proposals/0068-secret-provider-seam.md`.
