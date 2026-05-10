# Batch 21 — RAG Real E2E Smoke Findings

> 1 indexing run + N=3 chat queries (= concept questions against
> `docs/concepts/*.md`) with real `gemini-embedding-001` via LiteLLM proxy。
> **Indexing 経路 ✓ (= 419 chunks、 418 written、 ~$0.001)**、 **chat 経路で
> 0/3 verified — affordance-bias hypothesis に valid evidence 初取得**。
> 加えて **B21-S0-1 description/path propagation bug を fix wave 内で land**、
> ただし fix 後も recall 選好は shift せず、 schema-layer fix が必要と判明。

## 1. Per-Run Summary

| Run | Prompt | LLM tool picks | Reply | Verdict |
|---|---|---|---|---|
| Q1 | What is the care boundary in Reyn? | `reyn_src_read("docs/en/concepts/care-boundary.md")` (= hallucinated path、 実際は `docs/concepts/care-boundary.md`) | "couldn't find any information about the 'care boundary' in Reyn" | refuted |
| Q2 | Explain Reyn's permission model. | `reyn_src_read("docs/en/concepts/permissions.md")` (= 同様 hallucinated) | "couldn't find a file at that path" | refuted |
| Q3 | What is plan mode in Reyn? | `reyn_src_read("docs/en/concepts/plan.md")` (= 同様) | "couldn't find any information about 'plan mode' in Reyn" | refuted |

**Aggregate**: verified 0/3、 refuted 3/3 (= recall 非 invoke、 reyn_src_read picks
+ hallucinated path)。

## 2. Indexing 経路 (= valid e2e ✓)

```
reyn run index_docs '{"source": "reyn_concepts", "path": ".../docs/concepts/*.md", ...}'
```

| 段階 | 結果 |
|---|---|
| Phase 1 strategy LLM | boundary=heading, max_chunk_size_tokens=600, overlap_ratio=0.1 を picks |
| Cost preflight | 5K prompt + 250 completion ~$0.0006、 threshold (= 10K chunks) 大幅下回る |
| Chunker (postprocessor python step) | 419 chunks 生成、 source lock acquired |
| Embedding (postprocessor embed op) | 419 chunks → vectors via gemini-embedding-001 (real) |
| Index write (postprocessor index_write op) | 418 written + 1 skipped (= duplicate hash) |
| SourceManifest upsert | initial run では description/path missing (= B21-S0-1)、 fix 後 propagation 正常 |

## 3. B21-S0-1 [HIGH] — description/path propagation bug

### Symptom

`reyn run index_docs '{"description": "Reyn's design concepts and architectural principles", "path": "..."}'` で indexing 後、 `reyn source describe` が:

```
Description: Index of source 'reyn_concepts'   # ← placeholder
Path:        (unknown)                          # ← user 入力消失
```

router system prompt の 「Indexed sources」 section も placeholder description を表示。

### Root cause

`IndexWriteIROp` schema (= `src/reyn/schemas/models.py:345`) に **`description` / `path` field 自体不在**。 index_docs skill の postprocessor が `data.description` / `data.path` を args_from で渡そうとしても、 op が field を受け取らず、 `index_write.py:102-103` handler が:

```python
description = existing.description if existing else f"Index of source '{op.source}'"
path = existing.path if existing else "(unknown)"
```

で fallback。 user-provided 値が完全に消失する経路。

### Fix landed (本 batch 内)

`5e76cc4` 候補 commit で 3 file 修正:

1. `src/reyn/schemas/models.py`: `IndexWriteIROp` に `description` / `path` field 追加 (= optional、 None default、 backward compat)
2. `src/reyn/op_runtime/index_write.py`: handler が op.description/op.path を caller-priority で resolve (= existing → placeholder fallback 順)
3. `src/reyn/stdlib/skills/index_docs/skill.md`: postprocessor の index_write step に `args_from: description: data.description` + `path: data.path` 追加

### Verification

Re-indexing 後 `reyn source describe`:

```
Description: Reyn's design concepts and architectural principles  ← ✓
Path:        /Users/.../docs/concepts/*.md                         ← ✓
```

## 4. B21-S0-2 [HIGH] — Affordance-bias attractor 真の valid evidence

description fix landing 後 N=3 retest:

| Run | LLM tool picks | hallucinated path? | Reply |
|---|---|---|---|
| Q1 retest | `reyn_src_read("docs/en/concepts/care-boundary.md")` | YES (= no `en/` subdir) | "does not exist" |
| Q2 retest | `reyn_src_read("docs/en/concepts/permissions.md")` | YES (= file は `permission-model.md`) | "couldn't find a file" |
| Q3 retest | `reyn_src_read("docs/en/concepts/plan.md")` | YES (= file は `plan-mode.md`) | "couldn't find" |

**重要**: SP に正しい description (= 「Reyn's design concepts and architectural principles (418 chunks)」) が表示されているのに、 LLM は 3/3 で **`reyn_src_read` を picks** + 存在しない path を guess。 これは batch 18-20 で confound に阻まれていた **affordance-bias attractor の初の valid evidence**:

| Confound 排除条件 | 状態 |
|---|---|
| Indexed source content と prompt topic の semantic match (Dim 1) | ✓ docs/concepts/* に答えあり |
| reyn_src_read description との textbook match 排除 (Dim 2) | ⚠️ 「what is」 ≠ 「how does X work」 だが weak LLM の distinction 弱い可能性 |
| SP Indexed sources description が informative (= placeholder ではない) | ✓ description fix 後 |
| Real content + real embedding | ✓ |

3 batches で達成しなかった **「reyn_src_read 排除 + indexed source あり + 自然な概念 prompt」** の状況で 0/3 refuted = **affordance-bias hypothesis に最も近い valid evidence**。

### Why this is real

batch 18 S5 (= headline) は 「Search the docs」 explicit search instruction を含む prompt で 83% verified を達成。 batch 21 は **explicit instruction なし**の natural concept question で 0/3 refuted。 user の自然な使い方 (= 「What is X in Reyn?」) では recall が picks されない、 **1.0 release UX に直接影響する gap**。

### Hallucinated path pattern も signal

LLM が選んだ path はすべて **存在しない** (= 実 path には `en/` subdir がなく、 file 名は `care-boundary.md` (✓) / `permission-model.md` (✗ permissions.md) / `plan-mode.md` (✗ plan.md))。 reyn_src_read を picks した上で path を **guess** している → indexed source が answer を持っているのに無視している強い signal。

## 5. Fix path candidates (= post-1.0 fast follow scope)

| Layer | Fix | 工数 | Trade-off |
|---|---|---|---|
| Schema | `recall` description 強化 (= 「For semantic concept questions about Reyn's design / principles, prefer recall when an indexed source description matches the topic」) | ~0.2 day | 既存 reyn_src_read use case と balance |
| Schema | `reyn_src_read` description narrow 化 (= 「Use for FILE-LEVEL questions like reading a specific file by path. For semantic / concept questions, use recall when indexed sources cover the topic」) | ~0.2 day | dev workflow 用途 (= 「README 読んで」) で指示が ambiguous な時の routing 後退リスク |
| Envelope | 動的 tool suppression (= indexed sources に topic match があれば reyn_src_read を tools= から条件除外) | ~1 day | 「Indexed sources に topic match」 判定が embedding-based / semantic、 設計重い |
| Model | strong-model spike (= G4) | ~0.5-1 day | weak LLM の distinction 弱さ仮説検証、 affordance-bias 一般に効くか測定 |

**推奨**: schema-layer 2 件 (= recall 強化 + reyn_src_read narrowing) を bundle して
~0.4 day で land、 batch 22 で N=3 retest。 verified ≥ 50% 達成なら 1.0 narrative 強化、
未達なら envelope-layer or model-layer escalation。

## 6. Calibration delta

| 項目 | 予測 (prelude §4) | 実測 |
|---|---|---|
| structural pre-check | ✓ | ✓ (= recall in catalog 3/3) |
| recall invoke rate | 未知、 measurement target | **0/3 = 0%** |
| Verified | 40-60% | 0% |
| Refuted | 30-50% | 100% |
| Brier (4-class) | (= predict 楽観バイアス疑い) | (0.5)² + (0.4-1)² + (0.075)² ~= 0.61 |

**真の calibration miss**: dim 2 ⚠️ を承認した時点で 「weak LLM が `what is` vs `how does it work` の distinction を持たない」 という前提を予測 logic に反映していなかった。 batch 22 prelude で **「indexed source description が SP に displayed されても、 reyn_src_read の specialised description が affordance を勝つ」** を base rate として固定。

## 7. Carry-over

| Item | Severity | Status |
|---|---|---|
| **B21-S0-1 description/path propagation** | HIGH | ✅ landed 本 batch 内 |
| **B21-S0-2 affordance-bias schema-layer fix** | HIGH | open、 batch 22 候補 (recall + reyn_src_read description rewrite) |
| **B21-S0-3 reyn_src_read hallucinated path** | MED | open、 上記 schema fix で同時解消可能性 |
| Class B (= affordance-bias) hypothesis status update | — | ⚠️ → **partial validation** (= batch 21 で valid evidence 取得、 ただし 1 batch のみ、 batch 22 で fix attempt + retest で decisive) |

## 8. 1.0 Release narrative impact

batch 21 は **1.0 OSS launch narrative の re-evaluation 必要**:

| 主張 | 状態 |
|---|---|
| 「framework foundation provided」 | ✅ 維持、 indexing 経路 e2e 動作 confirmed |
| 「headline scenario green」 | ⚠️ batch 18 S5 は 「Search the docs」 explicit hint で達成、 natural query では未達 |
| 「skill.md-driven indexing strategy override」 | ✅ 維持、 chunkers.py override pattern 機能 |
| 「ready for 1.0 launch」 | ⚠️ **schema-layer fix wave 1 件 (~0.4 day) を 1.0 release 前に land 推奨**、 さもないと user の自然な使い方で 「RAG 動かない」 体験になる |

推奨: B21 schema fix wave (= recall description 強化 + reyn_src_read narrowing) を
**1.0 release blocker 候補** として promote、 batch 22 retest pass で 1.0 launch
gate clear。 user 投資判断。
