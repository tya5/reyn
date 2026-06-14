# CodeAct live-verify — design input for the holistic scheme re-design

**Author:** dogfood-coder (CodeAct author + efficacy live-verify) · **For:** e2e-coder
(protocol/architecture lead, scheme-seam owner) → lead design-review → coherent impl.
**Status:** input only. Owner steer: **no whack-a-mole** — fix the roots coherently;
PR #1617's 7 verified fixes are HELD as *evidence* (the optimal design supersedes /
integrates them), not merged as point-patches.

Foundational live-verify of the #1593 CodeAct scheme on flash-lite found an 8-defect
cascade that **every Fake-LLM unit passed**. The defects are not 8 independent bugs —
they collapse to **4 roots**. Each root below: primary evidence → why it is a root →
a coherent contract proposal (the *shape* of the fix, for e2e to own the seam).

All evidence is from `REYN_LLM_TRACE_DUMP` on fresh-agent runs against the main tree.

---

## Root 1 — catalog-shape contract (collapses defects #1, #3, #7)

**Primary evidence.** The live `SchemeOps.catalog_entries` adapter
(`router_loop.py:3148`) returns the **OpenAI-nested** shape:
`{"type": "function", "function": {"name", "description", "parameters"}}`.
Three CodeAct consumers assumed a **flat** `{name, description, parameters}`:
- `_render_code_api` read `entry["name"]` (top-level) → the SP code-API rendered
  `tool('')` ×50 (every action name empty). [#1]
- the `build_presentation` exclude filter read `entry["name"]` → the membership test
  never matched → a **silent no-op** (excluded actions still advertised = presentation
  /permission-parity leak). [#3]
- the in-code `tool()` dispatch gate checks `name in DispatchContext.tool_catalog`,
  where `self._catalog` is built from the **empty** `llm_tools_payload` → every
  `tool('file__read', ...)` rejected `"not in catalog"`. [#7]

**Why it's a root (not 3 bugs).** Three *different* notions of "the catalog" are
conflated, across two shapes:
1. **advertised tools** (`llm_tools_payload` → `self._catalog`) — what the LLM sees as
   JSON `tools=`. Empty for CodeAct (it advertises nothing; it writes code).
2. **dispatchable catalog** — what the per-call gate will allow. CodeAct advertises
   *nothing* but can dispatch *everything*. Coupling this to `llm_tools_payload` is the
   #7 root.
3. **render source** — what `_render_code_api` lists. Needs names + arg names.

enumerate-all "works" only because its `llm_tools_payload` *is* the full flat catalog,
so all three notions happen to coincide. CodeAct breaks the coincidence and exposes
that the contract is undefined.

**Coherent contract proposal.**
- Decouple **dispatchable catalog** from **advertised payload**. A scheme declares
  what is dispatchable independently of what it advertises as JSON tools. The
  `DispatchContext.tool_catalog` (the gate's membership set) is sourced from the
  scheme's *dispatchable* set, not from `llm_tools_payload`. (CodeAct: dispatchable =
  full flat catalog; advertised = none.)
- One canonical entry shape with explicit projections. `catalog_entries` returns a
  documented shape once; the OS provides the projections every consumer needs:
  `→ openai_tool_schema` (for `tools=`), `→ flat name/params` (for render + the
  dispatch membership map). No consumer hand-reads a nested dict at a guessed depth.
- This makes #1/#3/#7 *structurally impossible* rather than separately patched.

---

## Root 2 — harness IPC channel design (collapses defects #8, #6)

**Primary evidence.** The CodeAct harness writes its JSON **result envelope** to
`stdout` (`_codeact_harness.py`), and user code's `print(...)` *also* writes to
`stdout`. When the model wrote `print(tool('file__read', ...))`, the dict's Python
repr (single-quotes) landed on stdout ahead of the envelope → the parent's
`json.loads(stdout)` failed → `MalformedResponse` (the content was in the error text,
so the model salvaged it). [#8] Separately, the weak model frequently `print()`s
instead of assigning `result`, so the explicit result channel is often empty. [#6]

**Why it's a root.** The IPC multiplexes **protocol data** (the result envelope) and
**user-program output** (stdout/stderr) onto the *same* byte stream. Any snippet that
prints corrupts the protocol. This is a channel-design defect, not a serialization
bug.

**Coherent contract proposal.**
- Separate channels: protocol frames (the result envelope) travel on a dedicated fd /
  the existing control socket — **never** the stream user code can write to. User
  `stdout`/`stderr` are *captured* and returned **as data** inside the envelope.
- Define "snippet output" explicitly: `result` (explicit binding) is primary;
  captured `stdout` is a documented fallback/companion so a `print()`-style snippet
  still yields a usable observation. `format_feedback` decides precedence — the OS
  loop stays shape-agnostic (P7).

---

## Root 3 — SP engagement: scheme-owned SP (defect #2/#4/#5 surface + #1608②)

**Primary evidence.** CodeAct's live SP = the universal-category routing guide
(chars 0–4750, **~83%**) + the code-API fragment **appended** last (4754–5703, ~17%).
The dominant universal guide instructs `invoke_action`/`list_actions`/wrapper-
discovery, a `list_actions` MANDATORY-first rule, an "I am a Reyn agent" prose
preamble, and "reply directly" — all of which **contradict** CodeAct's contract
(emit one fenced ```python block calling `tool(...)`). Across 7 clean flash-lite runs
the weak model produced a *different* leak each time:

| run | output | leaked idiom |
|-----|--------|--------------|
| 1 | bare `tool('file__read',...)` no fence | universal direct-call |
| 2 | `Call: tool(...)` prose | prose-routing |
| 3 | ```json `{"tool_code": "..."}` | JSON-tool-call envelope |
| 4 | `print(tool(...))` not `result=` | print idiom |
| 5 | `I am a Reyn agent.` then unfenced code | identity-preamble rule |
| 6–7 | "unable to access file__read" giveups | wrapper-world assumption |

**Why it's a root.** The append-only `sp_fragment` channel (#1601) cannot *replace*
the dominant universal guide. The weak model follows the majority instruction. This is
exactly the deferred **#1608②** — and this evidence shows it is **essential, not
marginal**: it is the gating factor for weak-model CodeAct reliability.

**Coherent contract proposal (functional requirements; e2e owns the seam).**
- The OS exposes a **replace-capable SP channel**: a scheme can replace the
  routing-guide *tool-use region*, not only append a fragment. P7-clean: the OS
  exposes the region/channel; the *content* is scheme-owned in `build_presentation`.
- CodeAct content requirements: the code-API is the **sole** tool-use mechanism stated
  (remove invoke_action/list_actions/wrapper/`list_actions`-mandatory text — those
  tools don't exist under CodeAct, and their presence produces leaks 1–3); state the
  code-as-action contract **first/dominantly**; the identity rule must not force a
  prose preamble on the *act* turn (identity belongs in the terminal reply); teach
  "prose with no code block = terminal" (the interpret side already implements this).
- Validation: re-run the fresh-agent flake-retry live-verify and measure
  **fence-compliance lift** (fraction of turns emitting a clean fenced snippet) as the
  ②-success metric before the efficacy measurement resumes.

---

## Root 4 (meta) — test-shape fidelity

**Primary evidence.** The unit Fake `_CatalogOps` returned the **flat** shape while the
live adapter returns the **nested** shape. Because every CodeAct consumer was tested
against the flat Fake, **all 8 defects passed CI**. The shape mismatch *was* the reason
live-verify was load-bearing.

**Why it's a root.** A hand-rolled Fake whose output shape diverges from the live
producer makes integration defects invisible at the unit layer — the
fake-backend-misses-integration trap, here multiplied across consumers.

**Coherent contract proposal.**
- Single source of shape truth: the test Fake's output is derived from the **same
  projection** the live adapter uses (a shared constructor/fixture), so a shape change
  breaks producer and Fake together.
- A contract test asserts the live adapter's output shape matches the projection every
  consumer reads. (PR #1617 already moved `_CatalogOps` to the nested shape — that alone
  surfaced #3; the holistic design should make this *systematic*, not per-test.)

---

## Reference

PR **#1617** (HELD) implements point-fixes for all 8 defects and is **verified
end-to-end** (in-code `tool('file__read')` returns content cleanly, `MalformedResponse=0`,
2-turn terminate; 22 codeact + tier-audit + 73 dispatcher/scheme tests green). Use it
as the *behavioural oracle*: the holistic design is correct when these same outcomes
hold without the point-patches — i.e. the 4 roots, fixed coherently, make the 8
symptoms vanish by construction.
