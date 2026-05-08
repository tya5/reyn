# B5-H2 [HIGH]: 同じ error message、 異なる root cause が 2 つ

> 一行で: `eval.run_target` が `run_skill` IR op を path 形式で出して
> `KeyError: 'name'`、 と思ったら、 同じ error が **copy_to_work の 0-byte
> file** という別 root cause からも出ていた。 prompt fix で半分解消、 残り
> 半分は G2 preprocessor 化で解消。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | partial fix at `fe91321` (instruction clarify) → 残り root cause は **G2 fix `763c86c`** で解消見込み (batch 6 で再 verify) |
| Scenario | B (skill_improver chain) |
| Found | 2026-05-04 (batch 5 fix-verify) |
| Raw observation | [B5-B-skill-improver-chain.md](B5-B-skill-improver-chain.md) |

---

## 観測

batch 5 fix-verify Scenario B で skill_improver chain を起動。 期待は B4-H2
fix (`d9787cb`) で workspace が作成され、 eval cascade が score を出すこと
(prediction 90%)。 実際:

```
copy_to_work phase:
  → workspace 作成 ✅ (B4-H2 fix の効果)
  → ただし 0-byte file (これは別 finding B5R2-H2 として後で判明)

eval cascade:
  control_ir_failed: {
    "kind": "run_skill",
    "error": "'name'"
  }  ← 8 件 (4 eval run × 2 attempt)
  → 全 score 0.0
```

error の error_kind を見ても何の `'name'` か分からない。 当初は「`run_skill`
IR op handler が `name` field を期待しているが、 LLM が `path` で渡した」 と
解釈した。

## つまり何が起きたか (第 1 root cause)

`eval.run_target` phase の instruction を確認:

```
target skill in workspace
```

この wording で LLM が「target skill in workspace」 = path として解釈した
可能性。 `run_skill` IR op の args に **`skill` field** (= 正しい) ではなく
**`path` 形式 full path** を入れていた。 OS handler は `skill` field しか
読まないので、 `KeyError: 'name'` (= 内部実装で normalize されたエラー
message) が raise。

**第 1 修正** (`fe91321`): `eval.run_target.md` instruction を:

```
- **`skill` field** で target skill 名を渡す (e.g. `{"skill": "direct_llm"}`)
- **NOT** `name` / `path` / その他 field
- 誤 form 例: ✗ `{"path": ".reyn/skill_improver_work/.../skill.md"}`
```

instruction を厳密化。 prompt-only fix。

## 第 2 root cause 発覚 (batch 5 retest 2 で判明)

batch 5 retest 2 で `fe91321` fix 後を verify したところ、 instruction fix は
機能 ✅ ( = run_target が `skill:` field を出すようになった)、 ところが
**同じ `KeyError: 'name'` が persist**。

deeper investigation で別 root cause:

- `copy_to_work` が source content を read しているが、 LLM が **write op で
  content を omit** していた (= 0-byte file 生成)
- `parse_skill` が空 frontmatter を parse 試行 → `KeyError: 'name'` (= frontmatter
  の `name:` field 不在で raise)

つまり:

- **同じ `'name'` error が 2 つの root cause を共有**:
  - cause A: run_skill IR op の `skill` field 不在 → `fe91321` で fix
  - cause B: copy_to_work の 0-byte file → `parse_skill` が `name:` 取れない →
    G2 (`763c86c`、 preprocessor 化) で解消見込み

第 1 fix で半分解消、 残り半分は G2 で構造的解消。

## 影響

- error message の混同で **「fix が効いた / 効かない」 の判定が複雑化**:
  fe91321 fix が機能したのに同 error が出るので、 「fix 失敗」 と誤認しかけた
- skill_improver chain の信頼性検証が batch 5 fix-verify で完遂できず
- error message specificity の問題 (= context 含めず error_kind だけで identify
  しようとすると別 cause を区別不能) を露呈

## 修正

### 第 1 弾 (`fe91321`)
`eval/phases/run_target.md` の instruction を厳密化:
- `skill` field を **bold + 例示**
- `name` / `path` 等の誤 form を **誤例示として明示**
- `FileNotFoundError` 系 error 時の解釈ヒント追記 (= 「caller の copy step が
  完了してない可能性」)

### 第 2 弾 (G2 = `763c86c`、 別 finding)
`copy_to_work` phase を Phase Preprocessor 化 (= LLM 完全廃止)。 0-byte file
attractor が構造的に発生不可能に。

## 後続 (= batch 6 で再 verify)

- G2 fix 適用後の HEAD で skill_improver chain を再実行、 `KeyError: 'name'`
  が完全に消えるか確認
- error message を「`KeyError: 'name'` from parse_skill at <path>」 のような
  context-rich form に refactor (= observability 改善 wave 候補)

## 教訓

1. **同じ error message を異なる root cause が共有する設計は debug 効率を
   下げる**: error message に context field (= source / phase / op_invocation_id)
   を含める設計を OS 全体で見直すべき
2. **prompt fix と code fix を分けて検証**: instruction 修正で半分解消、
   structural fix で残り半分 — 修正の origin (= prompt vs code) を分けて
   verify すると root cause 切り分けが速い
3. **「FileNotFoundError なら copy step を疑え」 の知識を docs に**: skill
   開発者が cascade 失敗を見て困らないように、 typical failure mode と
   diagnosis hint を docs / runbook に integrate
4. **fix の effectiveness を 1 finding 単位で記録する**: 「fe91321 で何が
   直り、 何が直らなかったか」 を per-finding doc に lifecycle として記録、
   後から「同 finding に複数 fix」 の追跡を容易に
