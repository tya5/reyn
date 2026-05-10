# S4: Index Python source — finding doc

| フィールド | 値 |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `62fd21b` |
| Agent | `b17_s4` |
| Driver | `/tmp/s4_driver.py` (programmatic OSRuntime — Phase 1 only) |
| Sample size | N=3 |
| Model | `gemini-2.5-flash-lite` (LiteLLM proxy @ localhost:4000) |
| Target file | `src/reyn/op_runtime/embed.py` (184 lines) |
| **verdict 分布** | **verified: 3/3 (100%)** |
| **判定** | **R-RAG2 refuted — Phase 1 LLM valid、 副次発見 B17-S4-1 (postprocessor output schema mismatch)** |

---

## 1. サマリー表

| Run | boundary | max_chunk_size_tokens | min_chunk | overlap | chunk_count | extra_fields | schema_valid | verdict | elapsed |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `blank_line` | 800 | 50 | 0.0 | 3 | [] | true | verified | 5.2s |
| 2 | `blank_line` | 600 | 50 | 0.0 | 4 | [] | true | verified | 5.0s |
| 3 | `blank_line` | 600 | 50 | 0.0 | 4 | [] | true | verified | 6.1s |

全 3 run で `boundary: blank_line`、 enum 外フィールドなし、 schema valid。

---

## 2. Driver 設計 (programmatic Phase 1 only)

S4 の対象は Phase 1 LLM の ChunkStrategy 出力なので、 postprocessor を除いた
Phase 1 専用 driver を構築した。

```python
# Load skill + strip postprocessor so OSRuntime returns after Phase 1
skill = load_dsl_skill(SKILL_MD_PATH, skill_root=SKILL_ROOT)
skill = skill.model_copy(update={"postprocessor": None})

# OSRuntime with FakeEmbeddingProvider + AutoApproveInterventionBus
runtime = OSRuntime(
    skill=skill,
    model="standard",
    resolver=ModelResolver(MODEL_MAP),
    permission_resolver=perm,
    intervention_bus=_AutoApproveInterventionBus(),
    caller="dogfood_s4",
)
result = await runtime.run(initial_input=input_artifact)
# result.data == chunk_strategy artifact data (Phase 1 LLM output)
```

環境変数:
- `LITELLM_API_BASE=http://localhost:4000` (LiteLLM proxy)
- `OPENAI_API_KEY=dummy`
- `REYN_EMBEDDING_PROVIDER=fake` (embed op handler が FakeEmbeddingProvider を選択)

postprocessor なしにした理由: 副次発見として postprocessor の output schema 不整合バグ
(B17-S4-1) を発見した。 S4 本来のゴール (Phase 1 LLM ChunkStrategy 検証) とは独立した
別 layer のバグのため、 Phase 1 観測を分離して実施。

---

## 3. Phase 1 LLM 観測詳細

### 3-1. boundary 選択 (3/3 = blank_line)

Python source (`embed.py`、 184 行) に対して LLM は `blank_line` を一貫して選択した。

**根拠** (LLM が参照した context から推定):
- `gather_samples` preprocessor が `structure_hint: "Python with class/function definitions"` を返す
  (`chunkers.py:_detect_structure` が `.py` extension + `class /def ` パターンを検出)
- Phase `strategy` instructions は `blank_line` を「prose / scripts 向け」 と明記
- `heading` は Markdown 専用、 `sentence` は QA retrieval 向けと説明されており、
  Python code には blank_line が最も適合する

AST chunker は stdlib にないため `function` boundary は選択肢に存在しない。
LLM は適切に利用可能な enum 内から最適解を選んだ。

### 3-2. max_chunk_size_tokens のばらつき

| Run | 値 |
|---|---|
| 1 | 800 |
| 2 | 600 |
| 3 | 600 |

run 1 で 800、 run 2/3 で 600。 どちらも schema valid range (100–4000) 内。
phase instructions は「code / structured docs: 600–1000」 と示しており、
600–800 の範囲は instructions に整合的。

### 3-3. passthrough フィールド (echo 確認)

| フィールド | 入力 | 出力 | 一致 |
|---|---|---|---|
| source | `rag_code` | `rag_code` | ✓ |
| path | (absolute path) | (same) | ✓ |
| description | `"RAG embed op handler implementation"` | same | ✓ |
| mode | `append` | `append` | ✓ |

passthrough echo は全 run で正確。

### 3-4. 余分なフィールド (R-RAG2 hallucination probe)

全 run で `extra_fields: []`。 `function`、 `ast` など enum 外 boundary を
hallucinate した run はゼロ。 **R-RAG2 は本 scenario では発生しなかった。**

---

## 4. 副次発見: postprocessor output schema 不整合 (B17-S4-1)

Phase 1 を含む full OSRuntime.run() を試みた際、 postprocessor が実行後に
`PostprocessorError` で失敗した。

```
PostprocessorError: Postprocessor output failed schema validation (output_schema):
  'chunk_count' is a required property;
  'embedded_count' is a required property;
  'index_summary' was expected;
  'skipped_count' is a required property;
  'written_count' is a required property
```

### 原因

postprocessor は 3 ステップを実行し、 結果を artifact の sub-key に書込む:

```
data.chunk_stats  = {chunk_count: N, source_lock_acquired: True, ...}
data.embed_result = {embedded_count: N, skipped_count: 0}
data.index_result = {written: N, skipped: 0}
```

しかし `index_summary` output_schema が要求する top-level フィールドは:

```json
{"source": ..., "chunk_count": ..., "embedded_count": ..., "skipped_count": ..., "written_count": ...}
```

sub-key への書込みでは top-level に展開されない。 また `type` フィールドが
`chunk_strategy` のままで `index_summary` に更新されない。

### 影響

- `index_docs` skill の full e2e (Phase 1 → postprocessor chain) が常に失敗する
- G2 (postprocessor chain 完走 + SQLite write) は本バグにより **blocked**
- Phase 1 LLM 自体は正常動作 (S4 verdict: verified)

### 修正候補

**Option A** (推奨): `index_summary` 固定フィールドを skill.md の `into` path で
参照可能にする。 postprocessor に「assembly step」 を追加し、 sub-key から
top-level へ flatten する:

```yaml
postprocessor:
  steps:
    - type: python
      function: gather_samples  # ... existing steps
    - type: python
      module: ./chunkers.py
      function: assemble_summary  # NEW: top-level field mapping
      into: data  # overwrite the whole data dict
```

**Option B** (simpler): postprocessor の output_schema を実際の結果構造
(`data.chunk_stats.chunk_count` 等のネスト) に合わせて修正する。
ただし caller-facing API が sub-key 形式になるので使い勝手は悪い。

**Option C**: postprocessor 最終ステップ後に OS が artifact type を
`output_name` (`index_summary`) に書き換え、 step の `into=` パスに
`"data.chunk_count": "data.chunk_stats.chunk_count"` 的なマッピングを
サポートする (OS 拡張が必要)。

---

## 5. 新 bug

| ID | 重要度 | 内容 | 影響 |
|---|---|---|---|
| **B17-S4-1** | **HIGH** | `index_docs` postprocessor が `index_summary` output_schema を満たさない — sub-key への `into:` 書込みが top-level required フィールドに展開されない + `type` が `chunk_strategy` のまま | `index_docs` e2e 全 run で失敗 (postprocessor Phase は完走不能) |

---

## 6. セットアップ上の発見

### 6-1. InterventionBus が必須

`PermissionResolver` + `OSRuntime` の組合せで `intervention_bus` を渡さないと
起動時に例外:

```
permission_resolver requires intervention_bus on OSRuntime; wire one via Agent(intervention_bus=...)
```

dogfood driver は `_AutoApproveInterventionBus` を実装して渡す必要がある。

### 6-2. REYN_EMBEDDING_PROVIDER env var

`embed` op handler は `REYN_EMBEDDING_PROVIDER` env var で provider を切り替える
(デフォルト `litellm`)。 dogfood では `fake` を設定することで
`FakeEmbeddingProvider` を使う (Phase 1.5 dogfood まで OpenAI key 不在のため)。

---

## 7. Calibration delta

prelude S4 予測:

| 予測 | 実際 |
|---|---|
| verified: 50% | **100% (3/3)** |
| refuted: 30% | 0% |
| inconclusive: 15% | 0% |
| blocked: 5% | 0% (Phase 1 focus で postprocessor 切離し) |

Brier score (verified 予測 0.50):

```
B = (0.50 - 1.0)^2 + (0.30 - 0.0)^2 + (0.15 - 0.0)^2 + (0.05 - 0.0)^2
  = 0.25 + 0.09 + 0.0225 + 0.0025
  ≒ 0.365
```

予測より良い結果:
- LLM が blank_line を fallback として確実に選んだ (R-RAG2 attractor が弱い)
- Structure_hint が Python with class/function definitions を正しく伝えた

---

## 8. 結論

**Phase 1 LLM は Python source に対して robust** — enum 内の blank_line を
一貫して選択、 hallucination (R-RAG2) ゼロ、 passthrough echo 完璧。

**Postprocessor chain (G2) は B17-S4-1 でブロック** — postprocessor の
index_summary 組立ロジックが欠落している。 index_docs の e2e 完走には
このバグの修正が必要。

---

## 参照

- `src/reyn/stdlib/skills/index_docs/chunkers.py:_detect_structure` — Python structure_hint detection
- `src/reyn/stdlib/skills/index_docs/skill.md` — postprocessor steps (into パス定義)
- `src/reyn/stdlib/skills/index_docs/artifacts/index_summary.yaml` — expected output schema
- `src/reyn/kernel/postprocessor_executor.py:286` — output schema validation
- `/tmp/s4_results.json` — per-run raw data
- `/tmp/s4_driver.py` — driver script used
