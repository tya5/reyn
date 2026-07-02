# Tool-result offload — bird's-eye assessment (spec-level)

**Status:** design-first / pre-implementation. **GO-gated** (owner approval before build).
**Basis:** `origin/main` (verify with `git show origin/main:<path>`).
**Author:** e2e-coder. **Motivation:** owner — "仕様自体に穴があってモグラ叩きしてないか"
(is the offload *spec* itself holed, so we keep patching per-op?). The recurring offload bugs
(chat-MCP whole-envelope save; file_read offload-duplicating #2417) are symptoms of one spec hole.

## 1. The spec hole (root)

Tool results are **heterogeneous per tool** — each op returns a different dict:

| op | result fields (the "payload" field in **bold**) | marker |
|----|--------------------------------------------------|--------|
| mcp | `kind, status, server, tool, **content**, media_blocks, [structured]` | `content` |
| web_fetch | `kind, status, status_code, content_type, **content**, truncated, media_blocks, start_index, next_start, total_length` | `content` |
| web_search | `kind, status, **results**, …` | `results` |
| sandboxed_exec | `kind, status, backend, returncode, **stdout**, stderr, truncated` | `stdout` |
| file.read | `kind, op, path, status, **content**, [next_offset, _truncated, note, _self_bounded]` | (now truncates in-op, #2417 — no offload) |
| index_query / recall | `**chunks**, mode` | `chunks` |

The offload layer must decide **which field is the LLM-facing body** to store clean. It does this by
**guessing**: each op stamps a `_offload_payload_field` marker, and `decide_payload_field(result)`
honors it **IFF it is the SOLE oversized field** (`_oversized_fields(result) == [marker]`); otherwise
it returns `None` → the **whole dict is stored as one JSON-of-JSON envelope**.

**This guess breaks whenever a result has a SECOND large field**, or a missing/misplaced marker:

- **sandboxed_exec** with a huge stdout AND a huge stderr crash-trace → `decide` returns `None` →
  whole-envelope (stderr not clean-separable).
- **mcp** whose `content` AND `structured` (or `media_blocks`) are both oversized → `None` →
  **whole-envelope** — the owner's chat-MCP-call report. **tui confirmed the mechanism structurally**
  (no live needed): the marker IS set (`mcp.py` `_offload_payload_field: content`), so it is
  root **(A) two oversized fields** — the MCP server returns a large `structuredContent`, mapped to
  `structured` in the result, so `_oversized_fields == ["content", "structured"] != ["content"]` →
  `decide` returns `None`. The `router_loop` media-strip looks OUTSIDE the dispatch envelope, so it
  misses `structured` left inside `data`. This is the spec hole proven: a heterogeneous second field
  breaks the sole-oversized guess (案B's `attachments` removes `structured` from the offload decision
  entirely).
- **file.read** was offload-DUPLICATING an on-disk file until #2417 special-cased it to truncate —
  another per-op patch of the same hole.

The hole is that offload **infers structure it cannot know** from a heterogeneous, op-defined dict.

## 2. Current mechanism (two executors, one shared decision)

- **Decision (shared):** `decide_payload_field` + `_oversized_fields` (`context_builder.py`).
- **Primitive (shared):** `offload_value` (`services/offload/store.py`).
- **Executor ×2 (duplicated):**
  - **phase:** `offload_control_ir_result` → `_phase_preview_strategy` → `.reyn/control_ir_offload/`.
  - **chat:** `cap_tool_result_content` (`tool_result_cap.py`) → `MediaStore.save_tool_result` →
    `.reyn/tool-results/`.
  Each independently: unwrap the dispatch envelope, call `decide_payload_field`, store clean-or-whole,
  build a bounded preview + `_offload_ref` + `_offload_content_hash`. #2394 already showed these can
  diverge (chat lagged the control_ir fix) — the decision was extracted, but the **executors + the
  guess remain duplicated**.

## 3. Consumer analysis (what the LLM actually gets)

Inline the LLM receives: a **bounded preview** (per-field head+tail), `_offload_ref` (a file path it
**may `file.read`** for the full body — a scoped read is granted via `grant_offload_read`, a
`PermissionModel` method wired at `runtime.py` as `self._perm.grant_offload_read`),
`_offload_content_hash` (verified read-back), and `_offload_status/_offload_total_chars`.

- **Primary consumption = the bounded preview** (always present, always in-budget).
- **Deref (re-read the ref) is SUPPORTED but its real-world frequency is unmeasured** — OPEN
  QUESTION for the assessment: does the LLM meaningfully re-read offloaded refs, or is the preview +
  a re-fetch of the *origin* (file re-read / MCP re-call) sufficient? (Needs a dogfood-trace count;
  informs whether transient results must be stored at all, or just previewed + re-fetchable.)

## 4. 案B — canonical tool-result shape (recommended, spec fix)

**Normalize every tool result at the boundary** (the op adapter / MCP gateway / dispatch) into ONE
canonical shape the offloader never has to interpret:

```
CanonicalToolResult = {
    "text":        <the single canonical LLM-readable body — the ONLY thing offload truncates>,
    "attachments": [<typed non-text: media blocks, structured data — go to the media store>],
    "source_ref":  <re-fetch origin: {"path": …, "offset": …} for on-disk (file); None for transient>,
    "meta":        {<small structured status the LLM reads inline: status, url, returncode, …>},
}
```

Each op maps its heterogeneous dict → canonical **once, at its boundary** (mcp: content→text,
structured/media→attachments, source_ref=None; web_fetch: content→text, next_start→source_ref for
paging, media→attachments; exec: stdout→text, stderr→attachments (or appended to text with a marker),
source_ref=None; file.read: content→text, path+offset→source_ref).

**Offload becomes a single guessing-free rule** on the canonical shape:

1. `text` over budget → **truncate** to the budget with a `[truncated N/M chars — <how to get the
   rest>]` marker (the #2417 file_read form, generalized).
2. `source_ref` present (on-disk) → the "rest" is **re-fetch from origin** (path+offset) — **no copy
   stored** (the file already exists).
3. `source_ref` absent (transient: MCP/web/exec) → store the full `text` **content-addressed once**
   in the offload store + a ref; the "rest" is that ref.
4. `attachments` → the existing media store (unchanged).

`decide_payload_field`, `_oversized_fields`, the sole-oversized condition, and the six per-op
`_offload_payload_field` markers all **disappear** — there is no dict to guess a field from; `text`
is the payload by construction. `#2417`'s file_read truncate is exactly this rule for the file case.

**Why the owner report dissolves under 案B:** an MCP result's `text` is *always* the payload — there
is no "sole-oversized" check to fail and no whole-dict to fall back to. A large `content` is
truncated clean; `structured`/`media` are attachments; the JSON-of-JSON envelope **cannot occur**.

### Placement, migration, completeness
- **Normalization** lives at each op's boundary (return canonical), OR a thin `to_canonical(op_result)`
  adapter at the single dispatch seam (less op churn, one mapping table — recommended first step so
  op handlers change last).
- **One offload seam** function consumes only `CanonicalToolResult`; both chat + phase call it (thin
  callers), differing only in the store dir + budget.
- **Completeness gate (CI-enforced, MCP-gateway pattern):** grep-test that offload execution
  (`offload_value` / `save_tool_result`) is called **only** from the seam — a new path cannot
  re-introduce a bespoke offloader. And a shape-gate: the seam input is `CanonicalToolResult` (no
  raw op dict reaches it).
- **Migration** is incremental: introduce the canonical adapter + seam behind the two existing
  executors (byte-identical for single-payload results), then flip each op to canonical, then delete
  `decide_payload_field` + the markers. P7-safe (no skill vocabulary; op-kind mapping is OS-level).

## 5. 案A — consolidate executors only (fallback)

Merge the two executors into one seam (unwrap + `decide_payload_field` + `offload_value` + preview) +
the completeness grep gate, **keeping the guess model**. Also broaden `decide` so a multi-oversized
result stores its *dominant* field clean + the rest as preview (no data loss) instead of whole-dict —
which fixes the owner report symptomatically.

- **Pro:** surgical, low-risk, no op-handler churn, lands fast.
- **Con:** the spec hole **remains** — offload still guesses a field from a heterogeneous dict; the
  next op with an unusual shape re-opens it. It's the whack-a-mole containment, not the cure.

## 6. Recommendation + tradeoff

| | 案B (canonical) | 案A (consolidate) |
|--|------------------|--------------------|
| spec hole | **closed** (no guessing) | remains (guess broadened) |
| owner report | dissolves structurally | fixed symptomatically |
| blast radius | all op adapters + offload layer (staged) | offload layer only |
| risk | higher (contract change) | lower |
| future ops | canonical by construction | must remember a marker |

**Recommend 案B**, staged: (1) `to_canonical` adapter + one seam + completeness gate behind the
current executors (no behavior change); (2) migrate ops to canonical; (3) delete the guess. 案A is the
fallback if owner wants the fastest containment. Both eliminate the *duplicate-executor* divergence
(#2394 class); only 案B eliminates the *guessing* class.

## 7. Open questions for lead / owner
1. **Deref frequency** (§3) — measure whether the LLM re-reads offloaded refs; if rare, transient
   results may only need preview + origin-re-fetch, simplifying the store.
2. **exec stderr** — attachment, or appended to `text` with a `--- stderr ---` marker? (LLM usually
   wants both inline for a crash.)
3. **Staging** — land 案A's multi-oversized fix now (unblock owner) as step 0, then 案B? Or straight to
   案B behind the adapter?
4. Feed the **tui whole-envelope repro** (which second field made `decide` return None) to confirm
   §1's mechanism before build.
