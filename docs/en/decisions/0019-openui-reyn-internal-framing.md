# ADR-0019: OpenUI reframed as Reyn-internal contract

**Status**: Accepted (2026-05-04)
**Track**: Web UI framing fix (commit `b98272d`)

## Context

The web UI layer was originally documented as "OpenUI — a neutral
multi-vendor protocol for separating engine from design", with the
implication that other agent frameworks could adopt it as a shared
standard (mirroring how MCP became a cross-vendor LLM ↔ tool
protocol). The naming, governance language ("spec-first, neutral
naming"), and the path to lift `docs/openui/` into a standalone
`openui-spec` repository all reinforced that framing.

External review (the `tmp/external-review-*.md` series, contributed
by Claude Cowork 2026-05-03) raised two related points:

1. **Premature protocol claim.** A protocol becomes a protocol when
   independent adopters validate the abstraction. Calling it a
   neutral protocol with one host and one schema author is
   aspirational at best, hubristic at worst. MCP earned the title
   only after Anthropic + multiple model vendors + multiple tool
   ecosystems converged.
2. **Wrong headline value.** The actual differentiating product
   experience is the **App / Studio split** — two co-existing UIs
   from one engine state — which no other LLM agent stack ships.
   "Drop in a new design at runtime" is a secondary capability the
   layered contract makes possible, useful for org branding, but not
   the headline.

The original framing also drove implementation priorities that didn't
match user value: `reyn design` CLI, multi-design directory layout,
runtime picker — all engineering for a swap mechanism whose end-user
demand is uncertain.

## Considered alternatives

- **A. Keep the protocol framing, ship the swap mechanism.** Holds
  to the original ambition; risks the documentation overpromising
  and the implementation backlog distorting v0 priorities.
- **B. Reframe as Reyn-internal contract; deprioritise swap to
  v1.x.** Acknowledges the contract serves Reyn first, leaves the
  protocol claim as a future ambition earned through adoption,
  realigns v0 around the App / Studio split.
- **C. Drop the layered design entirely; inline the design in
  Reyn.** Throws away the headroom for future swap. Even if
  swap-day-one isn't the headline, the layered structure is cheap
  insurance.

## Decision

**Adopt B.**

Documentation changes (commit `b98272d`):

- `docs/openui/README.md`: removed "neutral multi-vendor" governance
  language. Added explicit "this is Reyn's web UI contract; the
  protocol claim is earned, not claimed". Section reordered so App /
  Studio split is the headline product value; design swappability is
  framed as a "secondary capability" the layered model enables.
- `docs/web/engine-design-contract.md`: AG-UI rationale rewritten
  ("headline value = App / Studio split, not design swap"). MCP
  rationale rewritten ("we borrow the layered contract style; we do
  **not** borrow MCP's neutral-protocol governance posture — that
  is earned by adoption").
- `docs/web/multi-design-selection.md` and `docs/web/design-distribution.md`:
  top banner added marking these as **Deprioritised to v1.x**. Reyn
  web v0 ships with one bundled design (`reyn-default`); the
  multi-design CLI / directory / runtime picker is an explicit v0
  non-goal. The documents are preserved as forward design so the
  Layer 0 contract stays "swap-ready" — but the implementation is
  out of v0 scope.

Implementation consequences:

- v0 ships exactly one design. The Layer 0 contract is live (= a
  future drop-in design works), but no `reyn design` CLI, no picker
  UI, no multi-design directory plumbing.
- Layer 0 + Layer 1 spec stays general-purpose so later swap remains
  cheap.

The `git mv docs/openui/ openui-spec/` path remains structurally
viable but is a hypothetical, not a roadmap item.

## Consequences

**Positive:**

- Honest framing matches what's been built and what differentiates
  Reyn.
- v0 work focuses on the App / Studio split (headline) rather than
  the swap mechanism (uncertain value).
- Future protocol claim is unblocked but not pre-claimed.
- Documentation tone shifts from "we're shipping a standard" to
  "we're shipping a product that has good extension hooks". The
  former requires social proof we don't have.

**Negative:**

- Loses some of the marketing pull that "open multi-vendor protocol"
  language carried. Replaced with a more substantive App / Studio
  pitch which (per external review) is the actually-differentiating
  story.
- Drafting cost: the deprioritised banners and rewrites are spread
  across four documents. Maintenance overhead if we later reverse
  course on swap priority — but the docs are clearly marked, so
  reversal is low-friction.

**Precluded:**

- Day-1 multi-vendor protocol marketing for OpenUI. Path remains
  open for the future, conditional on actual cross-vendor adoption.

## References

- Commit `b98272d` — documentation changes
- `tmp/external-review-*.md` (Claude Cowork analysis) — the external
  framing critique
- `docs/openui/README.md` — current canonical framing
- discussion-log Phase 14
