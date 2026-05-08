# B5-H1 [HIGH]: 過剰 consolidation regression — user feedback の善意で signal 弱化

> 一行で: 「肥大化、 重複、 過剰適合に気をつけましょう」 という user feedback を
> 受けて 4 rule を 2 段落に consolidation したら、 weak LLM が paragraph 内
> MUST を低優先扱いし、 specialist が再び list_skills 後に空 reply。 feedback の
> 方向は正しかったが implement で over-correct した。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `ca116f3` (re-balance: 5 個別 bullet × 2 MUST 復活、 wording dedup のみ維持) |
| Scenario | A (curry/specialist) |
| Found | 2026-05-04 (batch 5 fix-verify) |
| Raw observation | [B5-A-curry-recipe.md](B5-A-curry-recipe.md) |

---

## 観測

batch 5 fix-verify Scenario A は B4-H1 fix の effectiveness 検証目的で
specialist 経由 curry recipe を回した。 期待は「invoke_skill 到達 → narrator
reply → user に curry 届く」 (prediction 90%)。 実際:

```
specialist RouterLoop:
  list_skills("")              → ok
  list_skills("general")       → 10 skills (direct_llm 含む)
  agent_message_sent           ← 空 reply、 invoke_skill 呼ばず
```

**B3-H1 (= list_skills→stop attractor) が再発**。 B3-H1 は `48676ad` で fix
していたはず。 batch 4 retest では「invoke 到達 ✅」 と確認されたが、 batch 5 で
逆戻り。 user に「specialist から処理結果が得られませんでした」 が届く。

## つまり何が起きたか

時系列:

1. batch 1-3 で `router_system_prompt.py` に MUST rule を 4 件積み重ねた
   (F3+F9 / B2-H1 / B3-H1+M3)、 各 rule は個別 bullet × 各 1 MUST
2. **2026-05-04 中盤の user feedback**: 「肥大化、 重複、 過剰適合に
   気をつけましょう。 シナリオ間の実行結果の相互影響を減らす方法の一つに
   なるはず」
3. feedback を memory `feedback_prompt_design.md` に formalize
4. その後 `e90c0f2` で 4 rule を **2 段落に consolidation refactor**:
   - LOC -33% (= 39 行 → 26 行)
   - MUST count: 3 → 1
   - bullet 数: 4 → 2 (= 各 paragraph に複数 sentence)
5. batch 5 で B3-H1 attractor 再発を確認

deep dive の結論: weak LLM (gemini-2.5-flash-lite) は **paragraph 内の
複数 sentence 形式の MUST を 1 段落 = 1 priority signal** として扱う。
sentence 単位の MUST が個別に honor されない。

非対称性:

- **個別 bullet × 各 1 MUST × 複数 bullet** = priority signal N 個
- **段落 × N sentence × 1 MUST 含む** = priority signal 1 個

これは weak LLM 固有の特性で、 強モデルなら paragraph 内 MUST も honor する
見込み (= [G4](../../giveup-tracker.md) と連動)。 ただし Reyn の vision
(= weak LLM 路線) を維持する限り、 この非対称性を前提に prompt 設計する
必要がある。

## 影響

- multi-agent UX が再び崩壊 (= B3-H1 fix 前の状態に逆戻り)
- B4-H1 fix の e2e effectiveness 検証が prereq blocked で停止
- user feedback の意図 (= bloat 回避) と逆方向の regression を生み、 私の
  judgment への信頼を損ねた

## 修正 (`ca116f3`、 partial revert)

`e90c0f2` の consolidation を partial revert:

- **個別 bullet 維持** (= 5 bullet × 各 1 MUST 形式に戻す)
- ただし **wording 内 dedup は維持** (= 「engage the skill ecosystem」 jargon
  削除、 「browse list_skills before invoke_skill」 と「list_skills MUST
  invoke or describe」 の重複表現整理)
- LOC は元の 4 rule から 8 行縮小 (= dedup の効果のみ)
- LLMReplay fixture 7 entry rekey (= 既存 response 流用、 録音不要)

最終形:

```
- First decide intent (Action / Recall / Save / Forget / Reply),
  then pick tools from that group.
- Reply directly only for chitchat, questions about yourself, and
  clarifications back to the user. Domain tasks → Action.
- For Action or explicit-skill requests, call list_skills first,
  then invoke_skill (use describe_skill in between only when you
  need to inspect).
- After list_skills reveals at least one matching skill, you MUST
  call describe_skill or invoke_skill. Do NOT reply directly.
- After describe_skill, you MUST call invoke_skill or explain in
  text why not.
```

5 bullet × 2 MUST。 旧 4 rule の効果は維持しつつ、 重複 wording のみ整理。

## 教訓 (memory に formalize 済)

`feedback_prompt_design.md` の 2026-05-04 update section に追記:

> **balance:**
> - ✗ 4 rule × 各 1 bullet × 各 MUST × 重複表現 (= 旧形、 bloat)
> - ✗ 2 段落 × 各複数 sentence × 1 MUST (= consolidation 過剰、 信号が弱い)
> - ✅ 4 bullet × 各 1 MUST × wording dedup (= bullet 分離 + 言葉だけ整理)

## 後続 (= batch 5 retest 2 で部分 verify)

batch 5 retest 2 で `ca116f3` 適用後の挙動を確認:

- specialist が `list_skills → describe_skill` まで到達 ✅ (= 1 段階前進)
- ただし `describe_skill` 後に `invoke_skill` 呼ばず exit (= **B5R2-H1**、
  別 finding)

→ attractor は **三度目の variant** で再発。 prompt rule を bullet 単位で
増やす戦略の根本限界、 batch 6 検討項目として OS 層 state machine 検討
案件。

## 教訓 (process 観点)

1. **user feedback の方向 vs 過剰適用**: feedback は memory に formalize、
   ただし implement では「最小変更で方向に従う」 を優先する。 4 rule → 2 段落
   は明らかに over-correct
2. **fix wave に refactor を混ぜない**: feedback 由来の refactor (=
   consolidation) は **単独 PR** で landing し、 必ず dogfood verify を 1 回
   挟んでから次の fix wave に乗せる、 を batch 6 ルール化
3. **prediction 90% は自信過剰の sign**: fix-verify batch では fix 効果を
   high prediction で見積もりがちだが、 「fix した穴の脇から漏れる」 attractor
   variant の可能性を常に残す
4. **error message specificity の不足**: 「specialist から処理結果が得られ
   ませんでした」 が出た時点で「list_skills 後の attractor 再発」 か「他の
   原因」 か区別がつきにくい。 error context に「最後の tool_call」 「reply
   length」 等の field を含めると debug 効率上がる
