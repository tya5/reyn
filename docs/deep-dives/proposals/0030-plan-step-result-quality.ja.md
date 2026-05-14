# FP-0030: プランステップ結果品質 — よりリッチな出力ガイダンス

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

プランステップのシステムプロンプトは現在、「このステップが見つけたことを 1〜3 文で要約してください」と指示している。このハード上限により、ステップはコードスニペット・具体的な行番号・関数名・重要なデータを捨てることを強いられる — まさに合成ステップが高品質な回答を生成するために必要な詳細である。よりリッチな出力（コードスニペット、具体的な事実、構造化データ）を許可し、ソフトな文字数上限を設けたガイダンスに変更することで人工的な上限を取り除き、router が要約の要約ではなく実際の証拠から合成できるようにする。

---

## Motivation

### 現在のガイダンス（FP-0025 後）

```
Summarise what this step found in 1–3 sentences. Be factual; a separate
synthesis step will produce the user reply.
```

### 捨てられているもの

「auth.py の JWT デコードロジックは何をするか？」というタスクの場合:

| 現在のガイダンスでの出力 | 提案のガイダンスでの出力 |
|---|---|
| "auth.py には lines 78–95 に JWT デコードロジックが含まれる。" | 実際の関数シグネチャ、重要な行、挙動 |
| "session.py はセッション有効期限を管理する。" | 具体的なフィールド名、TTL 値、エッジケース |

合成 router は具体的な情報なしの要約を受け取る。コードスニペットを再現することができない。最終的な回答は必然的に曖昧になる。

### なぜハード上限でなくソフト上限か？

ハードな「1〜3 文」ルールはコンテンツタイプに関わらず切り詰めを引き起こす。ソフトな 800 文字上限:
- 10 行のコードスニペットをそのまま含めることを許可
- 合成コンテキストを肥大化させる壁文テキストのダンプを抑制
- ガイダンスで強制され、構文では強制されない — LLM は必要に応じて超過できる

---

## Proposed implementation

### 1. `build_plan_step_system_prompt` のガイダンスを更新（planner.py）

現在（FP-0025 後）:

```python
"Summarise what this step found in 1–3 sentences. "
"Be factual; a separate synthesis step will produce the user reply."
```

提案:

```python
"Report what this step found. "
"Include relevant code snippets, key facts, function names, line numbers, "
"or specific data directly — the synthesis step needs concrete evidence, "
"not paraphrases. "
"Keep your response under 800 characters where possible; "
"exceed the limit only when a code snippet or structured data requires it."
```

### 2. `src/reyn/tools/plan.py` の陳腐化した説明

`_PLAN_DESCRIPTION` にはまだ以下が書かれている:

```python
"The terminal step's text reply becomes the user-facing answer; "
"design the last step to synthesise"
```

これは FP-0025 C 以前は正しかったが、現在は陳腐化している — router LLM が `step_results` から合成を行い、ターミナルステップからではない。以下に更新:

```python
"After all steps complete, the router synthesises step results into "
"a final reply. Design each step to gather specific evidence "
"(code, facts, data); a dedicated synthesis turn handles the final reply."
```

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/planner.py` | `build_plan_step_system_prompt` のガイダンステキスト |
| `src/reyn/tools/plan.py` | `_PLAN_DESCRIPTION` の陳腐化したテキスト（FP-0025 後） |

---

## Dependencies

なし。ガイダンス変更のみ。

---

## Cost estimate

SMALL — 2 ファイルのテキスト変更。ロジック変更なし。

---

## Verification

1. Python ファイルを読むプランステップを実行 → `step_results` が 1 文の要約ではなく実際のコードスニペット/行番号を含む。
2. `plan.py` の `_PLAN_DESCRIPTION` がターミナルステップを合成者として参照しなくなっている。
3. 合成ステップがコードの証拠を含むより具体的な回答を生成する。

---

## Related

- `src/reyn/chat/planner.py` — `build_plan_step_system_prompt`
- `src/reyn/tools/plan.py` — `_PLAN_DESCRIPTION`
- FP-0025 (`0025-planner-narration-and-sp-fixes.ja.md`) — 本 FP が改善の対象とする合成分離を導入
- FP-0027 (`0027-plan-step-failure-transparency.ja.md`) — よりリッチなステップ結果により失敗のギャップがより可視化される
