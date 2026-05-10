# S5: Recall via Chat (HEADLINE) — Batch 17 Findings

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `62fd21b` |
| Scenario | S5 — recall via chat (headline scenario) |
| Sample size | N=5 |
| **Verdict breakdown** | **verified: 0 / refuted: 5 / inconclusive: 0 / blocked: 0** |
| **recall invoke rate** | **0/5 = 0%** (R-RAG1 confirmed at 100%) |

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 45% (2.25/5) | 0% (0/5) |
| refuted | 40% (2/5) | 100% (5/5) |
| inconclusive | 10% (0.5/5) | 0% (0/5) |
| blocked | 5% (0.25/5) | 0% (0/5) |
| recall tool invoked | 45%+ | 0/5 (0%) |
| reyn_docs in sources args | — | 0/5 |
| total elapsed (avg per run) | — | ~2.4s avg (3.0s, 2.2s, 0.9s, 1.1s, 5.0s) |

予測 Brier: E[B] = (0.45-0)² + (0.40-1.0)² + (0.10-0)² + (0.05-0)²
= 0.2025 + 0.3600 + 0.0100 + 0.0025 = **0.575**

実測 Brier: B = (0.45-0)² + (0.40-1.0)² + (0.10-0)² + (0.05-0)² = **0.575**

Brier delta: **+0.135** vs baseline random (= 0.44). Full-miss on verified/refuted axis.

---

## 2. Per-Run Details

| Run | Agent | Clean? | Tool Called (actual) | Reply summary | Verdict |
|---|---|---|---|---|---|
| 1 | b17_s5_run1 | Contaminated¹ | reyn_src_read(README.md) | Memory recall text (confused with indexed recall) | refuted |
| 2 | b17_s5_run2 | ✓ | reyn_src_read(README.md) | Memory recall text (list_memory / read_memory_body) | refuted |
| 3 | b17_s5_run3 | ✓ | NONE | `<ctrl42>` code-hallucination (B17-S5-1) | refuted |
| 4 | b17_s5_run4 | ✓ | NONE | `<ctrl42>` code-hallucination (B17-S5-1) | refuted |
| 5 | b17_s5_run5 | ✓ | NONE | `<ctrl42>` code-hallucination (B17-S5-1) | refuted |

¹ Run 1: agent `b17_s5_run1` had a probe turn ("Tell me what indexed sources you have access to.") injected before the S5 prompt due to a driver-side error. The S5 prompt was the second turn. The contamination did not change the verdict (both sub-pattern A and B yield refuted), but is noted for calibration purity.

Prompt (all runs):
```
What does the recall tool do? Search the docs.
```

### Run 2 reply (clean baseline, 193 chars):
```
The `recall` tool is used to read persisted facts from memory. You can use
`list_memory` to browse available memory entries and `read_memory_body` to
fetch the full content of a specific entry.
```

### Runs 3/4/5 reply (B17-S5-1 artifact, 63 chars each):
```
<ctrl42>call
print(default_api.reyn_src_read(path='README.md'))
```

### System prompt indexed sources section (confirmed visible to all agents):

Verified via probe turn on `default` and `b17_s5_run1`:
```
## Indexed sources (4 available)

- **rag_code** — Index of source 'rag_code' (8 chunks)
- **reyn_docs** — Reyn concept documentation (10 chunks)
- **test_drop** — Trial source for testing drop functionality (3 chunks)
- **test_source** — test (1 chunks)

Use the `recall` tool with `sources=[<name>, ...]` to search.
```

Despite this section being present, the `recall` (indexed docs) tool was invoked 0/5 runs.

---

## 3. What Happened

### Two distinct sub-patterns

#### Sub-pattern A — "recall" maps to memory system (runs 1, 2)

The LLM received the prompt "What does the recall tool do? Search the docs." and:
1. Called `reyn_src_read(path="README.md")` to look up Reyn's documentation (but for the wrong purpose — answering "what is recall" not "searching indexed docs")
2. Replied describing the memory recall system (`list_memory`, `read_memory_body`)

The word "recall" in the user prompt attracted the LLM to the "Recall" intent axis in the system prompt, which describes memory tools. The indexed sources `recall` tool (which uses the same word) was not invoked.

**Root cause**: The system prompt's "What you can do" section lists "Recall — read persisted facts / tools: list_memory / read_memory_body". The user prompt "recall tool" pattern-matches to this section, not to the indexed sources section ("Use the `recall` tool with `sources=[<name>, ...]`"). The ambiguity of the word "recall" is the attractor trigger.

Additionally, the LLM used `reyn_src_read(README.md)` to look up information about Reyn's architecture — treating "Search the docs" as "search Reyn's own documentation" rather than "use the indexed `recall` tool to search the indexed docs source".

#### Sub-pattern B — `<ctrl42>` code-hallucination (runs 3, 4, 5)

The LLM produced a reply containing:
```
<ctrl42>call
print(default_api.reyn_src_read(path='README.md'))
```

No `tool_called` event was emitted. The text was stored as an agent turn in history.jsonl. This appears to be a Gemini-flash-lite model artifact where the model attempts to invoke a tool via pseudo-code output instead of structured function-call format. The `<ctrl42>` prefix is a Gemini-specific encoding artifact.

**Effect**: The reply is nonsensical to the user. No indexed source search was performed. This is a distinct bug (B17-S5-1) but the verdict is still "refuted" — the indexed `recall` tool was not invoked.

### Infrastructure observations

#### SourceManifest in-memory cache issue (new bug B17-S5-2)

During setup, I discovered that the running `reyn web` server's in-memory `SourceManifest` singleton does not re-read from disk when another process writes new sources to `sources.yaml`. The `_cache` is set to `{}` on first access and never invalidated by external writes.

**Workaround used**: I verified the server re-read `sources.yaml` by checking a probe response that listed 0 sources (before seeding), then seeded sources, then confirmed a second probe showed all 4 sources. The server apparently re-read because its singleton's `_cache` was `None` at the time of the first probe — either the reload feature (uvicorn `--reload`) had just restarted the worker process, or the singleton had not yet been initialized. This was circumstantially confirmed.

**Risk**: If the server has already cached sources (or empty state), external seeding via `write_index_directly()` from a separate process will NOT be reflected until the server restarts. This is a production-relevant bug for live systems that add sources via CLI while the web server is running.

---

## 4. R-attractor Analysis

### R-RAG1 confirmed: recall tool invoke rate 0/5 = 100% refuted

This is the headline finding of batch 17. R-RAG1 (recall tool invoke 忘れ) is now confirmed at the same 100% rate as the batch 16 plan-mode invoke rate (0/25 = 0%). The optimistic hypothesis — "recall is more concrete than plan (= 'search docs' → direct mapping to recall tool)" — was incorrect.

The dual-meaning attractor of "recall" is the primary cause:
1. The system prompt uses "Recall" as an intent label for memory operations
2. The indexed sources section uses "`recall` tool" for RAG search
3. The user prompt "recall tool" maps 100% to the memory intent, 0% to the indexed sources search

This is structurally identical to the batch 16 R1 attractor: the user's intent is clear to a human reader but the LLM's routing is dominated by the system prompt's intent vocabulary.

### Comparison to batch 16 plan-mode

| Metric | Batch 16 (plan) | Batch 17 S5 (recall) |
|---|---|---|
| Invoke rate | 0/25 = 0% | 0/5 = 0% |
| Primary attractor | text-reply from training data | memory intent section |
| LLM model | gemini-2.5-flash-lite | gemini-2.5-flash-lite |
| Tool visibility | tool registered, not in SP | tool registered, mentioned in SP |

Key difference: unlike plan-mode, the `recall` tool IS mentioned in the system prompt (`## Indexed sources` section: "Use the `recall` tool..."). Despite this, the invoke rate is 0%. This strengthens the diagnosis: it's not a tool-registration issue but a **vocabulary collision attractor** between "Recall" (memory intent) and `recall` (indexed search tool).

---

## 5. New Bugs

### [HIGH] B17-S5-1: Gemini flash-lite emits `<ctrl42>` pseudo-code instead of tool calls (3/5 runs)

| 項目 | 詳細 |
|---|---|
| ID | B17-S5-1 |
| 重要度 | HIGH (= renders agent reply nonsensical to user; 60% rate in this sample) |
| 現象 | The LLM produced `<ctrl42>call\nprint(default_api.reyn_src_read(path='README.md'))` as a text reply instead of making an actual `reyn_src_read` tool call. No `tool_called` event emitted. The reply is stored as an agent turn in history.jsonl. |
| 証拠 | Runs 3, 4, 5: history.jsonl agent turn = `<ctrl42>call\nprint(default_api.reyn_src_read(...))`. Events: only `user_message_received` + `compaction_check`, no `tool_called`. |
| Root cause hypothesis | Gemini-flash-lite is emitting a function-call in pseudo-code format (likely a model-version regression or schema mismatch in the LiteLLM proxy's tool-call format parsing). The `<ctrl42>` prefix may be a Gemini-internal encoding character (Unicode control character at U+001A or similar) that leaks into text output. |
| Impact | 3/5 runs produced nonsensical replies. The router did not detect this as an error (no empty-response detection fired, no `is_empty_router_response()` trigger). |
| Fix direction | Add detection in `_is_empty_router_response()` or a new `_is_malformed_router_response()` check for replies that look like code-block tool-call hallucinations (e.g., starts with `<ctrl` or contains `default_api.` pattern). Alternatively, investigate if the LiteLLM proxy's function-call schema for Gemini models needs a schema version update. |
| scope | `src/reyn/chat/router_loop.py` + LiteLLM proxy config |

### [HIGH] B17-S5-2: SourceManifest in-process cache not visible to external writers

| 項目 | 詳細 |
|---|---|
| ID | B17-S5-2 |
| 重要度 | HIGH (= production-relevant: `reyn run index_docs` writes from CLI while `reyn web` is running; web server won't see new sources until restart) |
| 現象 | The `reyn web` server's `SourceManifest` singleton uses an in-memory `_cache` that is only updated when the manifest's own process calls `upsert()` or `remove()`. External writes to `sources.yaml` (by a separate CLI process) are not reflected until `_cache` is `None` (fresh server start or singleton re-init). |
| 証拠 | Setup: server had `_cache={}` (or pre-existing empty state). After seeding from a separate Python process, the server initially showed 0 sources. The server only picked up the new sources when its singleton's `_cache` happened to be `None` at the time of the first request (likely due to worker restart from uvicorn `--reload`). |
| Fix direction | Phase 1 quick fix: add file-mtime polling in `get_all()` (re-read if `sources.yaml` mtime > last-read mtime). Phase 2: use `inotify`/`kqueue` for cross-process notification (per ADR-0033 Phase 2 plan). Quick fix: `SourceManifest.get_all()` checks `os.path.getmtime(self._path)` against a stored `_last_mtime`; if changed, calls `self.load()`. |
| scope | `src/reyn/index/source_manifest.py` — `get_all()` method |

### [MED] B17-S5-3: "recall" vocabulary collision — memory intent vs indexed sources tool

| 項目 | 詳細 |
|---|---|
| ID | B17-S5-3 |
| 重要度 | MED (= S5 headline scenario unverifiable until fixed; affects UX for any prompt containing "recall") |
| 現象 | The system prompt uses "Recall" as an intent label for memory operations AND uses "recall tool" in the indexed sources section. When the user says "recall tool", the LLM maps it 100% to the memory intent, never to the indexed sources search tool. |
| 証拠 | 5/5 runs: LLM replied about `list_memory`/`read_memory_body` instead of invoking `recall(query=..., sources=[...])`. Run 1 probe explicitly listed `reyn_docs` as an available source; the LLM acknowledged it but still did not invoke `recall` for the S5 prompt. |
| Fix candidates | (a) Rename the system prompt intent label "Recall" to "Memory" (= "Memory — read persisted facts / tools: list_memory / read_memory_body"); (b) Add `recall` to the "What you can do" section explicitly ("I can search indexed documents via the `recall` tool"); (c) Add a Behaviour rule that disambiguates: "For indexed-source search, use the `recall` tool with `sources=[<name>]`. For memory retrieval, use `list_memory` / `read_memory_body`."; (d) Rename the `recall` tool to `search_index` or `search_docs` to avoid the vocabulary collision with memory recall. |
| Best fix | Option (a) + (c): rename intent label to "Memory" and add an explicit disambiguating Behaviour rule. This avoids the vocabulary collision without renaming the tool (= API surface change). |
| scope | `src/reyn/chat/router_system_prompt.py` — intent axis section + Behaviour section |

---

## 6. Calibration Notes

### Prediction miss: verified 45% → actual 0%

The pre-batch optimism ("recall is more concrete than plan → higher invoke rate") was wrong. The "concrete mapping" hypothesis assumed "Search the docs" → `recall` tool. In practice, the word "recall" in the user prompt is a stronger attractor to the memory intent label than to the indexed search tool.

Key calibration update: **vocabulary collision attractors are as strong as R1-family attractors** even when the tool is explicitly mentioned in the system prompt. The issue is not tool visibility but semantics.

### Next batch calibration

For S5 re-run after B17-S5-3 fix (rename "Recall" intent → "Memory"):
- verified: 30% (recall tool invoke uncertain; model may still text-reply from indexed sources section)
- refuted: 50% (attractor partially resolved but "Search the docs" may still miss)
- inconclusive: 15%
- blocked: 5%

For S5 re-run after B17-S5-1 fix (code-hallucination guard):
- The B17-S5-1 3/5 rate should drop to 0/5; runs 3-5 would then show sub-pattern A behavior instead

### Brier score

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 45% | 0/5 (0%) | (0.45-0)² = 0.2025 |
| refuted 40% | 5/5 (100%) | (0.40-1.0)² = 0.3600 |
| inconclusive 10% | 0/5 (0%) | (0.10-0)² = 0.0100 |
| blocked 5% | 0/5 (0%) | (0.05-0)² = 0.0025 |
| **Brier score** | — | **0.575** |

Prior calibration: batch 16 S1 was 0.70 (total miss). S5 batch 17 = 0.575 (slightly better — refuted was correctly predicted at 40%, only miss was verified vs actual 0%). The recall-via-chat scenario is harder than expected.
