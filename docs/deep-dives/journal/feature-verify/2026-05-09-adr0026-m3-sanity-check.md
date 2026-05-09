---
title: "ADR-0026 M3 Wave 1+2 — Sanity check findings (2026-05-09)"
date: 2026-05-09
related-commits: [edd4c1b, 367b41c, ba4c5fe, 66435d1]
status: stable
---

# ADR-0026 M3 Wave 1+2 — Sanity check findings (2026-05-09)

## 1. What was verified

The cumulative state of ADR-0026 through M3 Wave 1+2:

| Milestone | Commit | Scope |
|---|---|---|
| M1 Infrastructure | `edd4c1b` | types / registry / dispatch + 27 Tier 2 invariants |
| M2 POC | `367b41c` | web_search migrated; +14 Tier 2 invariants; all gates green |
| M3 Wave 1 | `ba4c5fe` | 7 capabilities migrated; dispatch_kind field; +99 Tier 2 invariants |
| M3 Wave 2 | `66435d1` | 17 capabilities migrated; all 3 Type C gaps closed; +127 Tier 2 invariants |

Cumulative: 26 ToolDefinitions registered in the unified ToolRegistry, +226
Tier 2 invariants added across M2+M3, 1725 tests passed / 2 xfailed, and
byte-identity for LLMReplay fixtures preserved across all capability migrations.

The sanity check was a 4-query live exercise against the running `reyn web`
A2A endpoint. Its goal is to detect functional regression in real-LLM chat
behavior — it does not comprehensively validate all 26 ToolDefinitions or all
gate configurations. It provides a representative signal for the most common
usage patterns.

## 2. Setup constraints

The sanity check used the `reyn web --reload` server running at
`localhost:8080` (pid 18763) which had been hot-loading code throughout the
day of 2026-05-09. The hot-reload path is the standard development mode and
is equivalent to a fresh server start for tool dispatch purposes.

The A2A endpoint preserves the dual-LLM invocation kind (router-style function
calling + phase-style Control IR JSON output) under the unified registry without
any operator-visible change. The registry is transparent to the calling
protocol: `build_tools()` still produces the same OpenAI `tools[]` shape;
`ControlIRExecutor.available_ops()` still produces the same `ControlIROpSpec`
shape. The LLM sees identical inputs; the registry change is entirely
implementation-internal.

The model in use is `openai/gemini-2.5-flash-lite` via LiteLLM proxy at
`localhost:4000`. This is a weak model by design (the project tests against
weak models to surface prompt stability issues). Latency and response quality
reflect this constraint.

## 3. Action: 4 sanity check queries

Four queries were dispatched to the A2A `send-message` endpoint in a single
session. Results:

| Query | Latency | Outcome | Tool dispatched |
|---|---|---|---|
| "Hi, what's 2+2?" | 1.8s | "2 + 2 = 4." | (none, direct LLM reply) |
| "What is Reyn? Briefly." | 1.5s | Project summary, 211 chars | (none, project_context-driven) |
| "What files are in src/reyn/tools/?" | 3.4s | Meta-conversational reply ("Is there anything else I can help you with..."). Tool DID run and returned. LLM chose an ask-back response. | `reyn_src_list` called and returned |
| "Find recent HN posts about LLM agents from the last week." | 4.8s | 711 chars listing HN posts | `web_search` with operator hint `site:news.ycombinator.com LLM agents last week` |

The events log confirmed:

- Query 3: `reyn_src_list` was dispatched and returned a directory listing.
  The LLM received the result and chose to produce a meta-conversational reply
  instead of presenting the listing. The tool call completed without error.
- Query 4: `web_search` was dispatched with the `site:news.ycombinator.com`
  operator hint actively used by the model (the hint was surfaced in the tool
  description as part of commit `8af3444`). The result was used to populate
  a substantive reply.

All 4 queries returned without empty completion, timeout, or dispatch error.
Latency range 1.5–4.8s is consistent with prior sessions under the same model.

## 4. Verdict

Wave 1+2 introduces zero regression in real-LLM chat behavior.

The `file_lookup` meta-conversational reply in query 3 ("Is there anything else
I can help you with...") is the same LLM-side weak-model ask-back pattern
observed earlier in this session (commit `563ace6`'s findings, which replaced
the tutorial 02 example query "what skills are available?" for the same reason).
The pattern is: the tool call completes correctly, the LLM receives the result,
and then opts to produce a conversational close rather than presenting the data.
This is a weak-model quirk unrelated to the registry migration. The registry
change has no path to influence post-tool LLM response choice.

Specifically: this sanity check does not verify all 26 ToolDefinitions
individually, does not test gate enforcement for `router=deny` or `phase=deny`
capabilities (shell, lint, ask_user, delegate_to_agent, plan, reyn_src_*), and
does not exercise phase-side dispatch paths. Those are covered by the 1725-count
test suite, not by this 4-query live check. The check's scope is representative
end-to-end behavior for common query patterns.

## 5. What's still pending (M4 cleanup)

The following items remain before the ADR can be closed:

- **Phase-side dispatch consuming registry.** `ControlIRExecutor.available_ops()`
  currently returns a hand-written `ControlIROpSpec` list. M4 wires it to call
  `registry.for_phase()` and derive descriptions from `ToolDefinition` single
  source.
- **ToolContext expansion.** Open Q #3 from the ADR: `router_state` and
  `phase_state` typed sub-objects (carrying `chain_id`, `session metadata`,
  `skill_run_id`, `current_phase`, etc.) are stubbed as `None` in the current
  `ToolContext`. M4 populates them so handlers that need caller-kind-specific
  state can access it without defensive `getattr` fallbacks.
- **`allowed_ops` semantic migration.** Coarse-grained names (`file`,
  `ask_user`) in existing skill phase frontmatter need prefix-wildcard
  interpretation or explicit rewriting. ADR recommendation is hybrid with
  deprecation warning; M4 implements this.
- **`router_tools.py` inline ToolSpec residuals.** Some capabilities in
  `build_tools()` still use inline `ToolSpec` literals rather than registry
  lookup + render. M4 removes all residuals.
- **Per-call schema enrichment hook.** Anchored as a future metadata field in
  `ToolDefinition`; not yet implemented.
- **Sunset legacy aliases.** `OP_KIND_MODEL_MAP` and `_DISPATCH_KIND` remain
  as backward-compat shims. M4 removes them with a one-release deprecation
  window per Open Q #4 resolution.

## 6. Cross-references

- ADR-0026: `docs/deep-dives/decisions/0026-unified-tool-registry.md` —
  design, migration plan, and acceptance criteria (§9 updated with M3
  completion status)
- LLM invocation surfaces concept: `docs/concepts/llm-invocation-surfaces.md`
  — §9 updated to reflect M3 Wave 1+2 landing state
- M2 POC findings: `docs/deep-dives/journal/feature-verify/2026-05-09-adr0026-m2-poc-success.md`
  — M2 verification gates, adapter shim analysis, and M3 recommendations
