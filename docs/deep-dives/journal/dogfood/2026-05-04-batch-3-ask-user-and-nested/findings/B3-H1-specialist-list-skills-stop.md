# B3-H1 [HIGH]: specialist、 list_skills の手前で停まる — F3 の亡霊、 1 段階前へ

> 一行で: B2-H1 fix で `describe_skill → 停止` を塞いだら、 同じ attractor が
> `list_skills → 停止` という 1 段階手前で再発した。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `48676ad` → consolidation `e90c0f2` で signal 弱化 (B5-H1 regression) → **re-balance** `ca116f3` で final |
| Scenario | S1 (multi-agent re-confirm) |
| Found | 2026-05-04 |
| Raw observation | [B3-S1-observation.md](B3-S1-observation.md) |

---

## 観測

S1 で specialist agent (= B3-M1 修正後の setup で作成) に「カレーの簡単な作り方」 を delegate。 batch 2 の B2-H1 fix (`83bad83`) で `router_system_prompt.py` に「`describe_skill` 後は `invoke_skill` か明示的説明」 ルールを追加していたので、 invoke_skill まで到達する想定だった。

WAL の tool_call sequence:

```
list_skills("")          → 10 skills in general (including direct_llm)
list_skills("general")   → 10 skills listed
agent_message_sent       ← router loop 終了、 invoke_skill / describe_skill 呼ばず
```

`describe_skill` を **経由せず**、 `list_skills` の結果を見ただけで「自分は
何もしない」 と判断する pattern が出現。 reply は空、 default は B2-H2 fix
path で「specialist から処理結果が得られませんでした」 を user に提示。

## つまり何が起きたか

B2-H1 fix は **describe → 何かの遷移** を MUST で縛ったが、 **list → describe
の遷移** は野放しだった。 weak LLM (gemini-2.5-flash-lite) は MUST rule で
明示された経路 (= describe 後 invoke or 説明) には従うが、 **MUST が付いていない
段階で停止** する判断を取った。

attractor は「fix した穴を塞いだ脇から漏れる」 形で variant 化する。 B2-H1 +
B3-H1 の 2 件で「list → describe → invoke の 3 段 chain で各 transition を
gate しないと詰まる」 ことが明確化。

これは batch 2 retrospective でも触れた「**fix が効いた領域は別経路で
再発する**」 という pattern。 batch 3 prediction の「外れ予測 = chain 接続で
新問題」 とは予測したが、 attractor の **variant 化** までは予測しきれなかった。

## 影響

- **multi-agent UX 崩壊**: specialist 経由で何を頼んでも空 reply → default が
  「peer 失敗」 を user に提示、 multi-agent の有用性が破綻
- B2-H1 fix の effectiveness が部分的にしか効果出ず (= 「fix した」 と
  「使えるようになった」 のギャップが顕在化、 batch 3 retro で議論)
- 後続の B3-M3 (= default 側で同 family attractor) も同根、 同 fix で吸収予定

## 修正

### 第 1 弾 (`48676ad`、 batch 3 fix wave)

`router_system_prompt.py` の Behaviour section に B2-H1 ルールの直後へ追加:

```
- After list_skills reveals at least one matching skill, you MUST call
  describe_skill (to inspect) or invoke_skill (to execute). Do NOT reply
  directly when a relevant skill is available; engage the skill ecosystem.
```

LLMReplay fixture 7 entry を rekey (= 既存 response 流用、 録音不要)。 1 Tier 2
test (`test_post_list_skills_must_invoke_or_describe`)。

### 第 2 弾 (`e90c0f2` → `ca116f3`、 prompt consolidation の partial revert)

user feedback 「肥大化、 重複、 過剰適合」 を受け、 4 rule を 2 段落に
consolidation refactor (`e90c0f2`)。 ただし weak LLM が paragraph 内 MUST を
低優先扱いし B5-H1 regression が発生 (= batch 5 で specialist 再び list_skills
後空 reply)。

`ca116f3` で partial revert: 個別 bullet 維持 (= 5 bullet × 各 1 MUST)、
wording のみ dedup (= jargon 削除 + 重複表現整理)、 LOC 31 行縮小だが信号
強度は維持。 7 entry 再 rekey。

## 後続 (= batch 5 retest 2 で発覚)

batch 5 retest 2 で B3-H1 (`ca116f3`) 適用後の挙動を再観測:

- specialist が `list_skills → list_skills → describe_skill` まで進む
  (= 1 段階前進)
- ただし `describe_skill` 後に `invoke_skill` 呼ばず exit (= **B5R2-H1**、 別
  finding 化)

→ attractor の **三度目の variant** が出現。 single bullet 単位で対処を
続けると bullet 数が線形増加、 cross-scenario interference が増える
(= memory `feedback_prompt_design.md`)。 構造的解は OS 層で「discovery 後の
状態遷移を gate」 する設計、 batch 6 検討項目。

## 教訓

1. **attractor は塞いだ穴の脇から漏れる**: 1 つの transition を MUST で
   縛っても、 上下の transition で発症する
2. **prompt rule を線形に追加する戦略には限界**: bullet 数が増えると weak
   LLM への signal flooding を生み、 別 regression を誘発 (= B5-H1)
3. **構造的解は OS 層の state machine**: discovery (`list_skills` /
   `describe_skill`) 後の state を OS で track し、 LLM の text reply 試行を
   gate する設計案が batch 6 以降で必要。 prompt rule 追加路線は対応 lag を
   生み続ける
