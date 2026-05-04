# B5R2-H1 [HIGH]: describe_skill→stop、 attractor の三度目の variant

> 一行で: B5-H1 fix で list_skills → describe_skill までは進むようになったが、
> 今度は describe 後に invoke せず exit する pattern が出現。 batch 2 の B2-H1
> と **同じ位置で 3 度目の発生**。 単 bullet 単位の対症療法には限界がある、
> という事実が確定した瞬間。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | open — batch 6 で OS 層 state machine 検討 |
| Scenario | A (curry/specialist retest 2) |
| Found | 2026-05-04 (batch 5 retest 2) |
| Raw observation | [B5R2-A.md](B5R2-A.md) |

---

## 観測

batch 5 retest 2 で B5-H1 fix (`ca116f3`、 prompt re-balance) 適用後の挙動を
verify。 期待は「list_skills → describe_skill → invoke_skill 全 chain 通過」
(prediction 高) 。 実際:

```
specialist RouterLoop:
  list_skills("")              → ok
  list_skills("general")       → 10 skills
  describe_skill("direct_llm") → ok ✅ (= B5-H1 fix で 1 段階前進)
  agent_message_sent           ← invoke_skill 呼ばず exit
```

`describe_skill` まで到達したが、 そこから先で invoke せず空 reply で exit。
B5-H1 fix の効果は「list 後 describe」 の transition のみで、 「describe 後
invoke」 の transition は依然 attractor が残る。

## つまり何が起きたか — attractor の三度目の出現

attractor の history を時系列で整理:

| Stage | Attractor | Fix |
|---|---|---|
| batch 2 (B2-H1) | `describe → 停止` | `83bad83` 「describe 後 invoke or 説明」 rule 追加 |
| batch 3 (B3-H1) | `list → 停止` | `48676ad` 「list 後 describe or invoke」 rule 追加 |
| batch 5 fix-verify (B5-H1) | B3-H1 が consolidation で破壊 | `ca116f3` で revert |
| **batch 5 retest 2 (B5R2-H1)** | **`describe → 停止` 再発** | open |

つまり B2-H1 と **同じ位置** (= describe 後 invoke せず) で attractor が
3 度目の発生。 `83bad83` の MUST rule は今も prompt に残っているにも関わらず、
weak LLM がそれを honor しないケースが出てきている。

仮説 (= deep investigation 必要):

1. `83bad83` rule の wording が `ca116f3` re-balance で微妙に変わった可能性
2. weak LLM が「rule は知っているが、 状況によって従わない」 確率的挙動
3. context length / prior turns の影響で MUST signal が rear-end で弱まる

→ いずれにせよ **prompt rule 追加路線では押さえきれない領域** に入った
ことが確定。

## 影響

- multi-agent 完全動作の信頼性が **prompt 依存では確保不可能**
- B3-H1 / B5-H1 fix の累積効果が boundary で止まる現象を確認
- attractor の variant が無限に湧く可能性、 prompt rule の対症療法サイクル
  からの脱出が batch 6 以降の戦略課題

## 修正候補 (open、 未着手)

option A: **OS 層 state machine で discovery 状態を track**
- `_router_loop_state` に `discovered_skills: list[str]` を保持
- `list_skills` / `describe_skill` 後の state を OS 層で記録
- 次の LLM call で「discovered ≥ 1 かつ invoke_skill 未呼び」 なら強制
  prompt injection で「invoke or explain」 を inline で push (= LLM の
  current turn output に influence を即座に加える)

option B: **agent_message_sent を delay**
- `list_skills` / `describe_skill` 直後の text reply 試行を RouterLoop が
  delay
- 「discovered skills を活用しましたか?」 を 1 回 prompt し直す追加 turn

option C: **強モデル併用 trigger 発火** ([G4](../../giveup-tracker.md))
- weak LLM 路線を諦めて strong model 併用、 attractor 自然消失を期待

option D: **status quo 受容**
- attractor は確率的に発生する、 1/10 失敗を許容して runtime retry で
  recover する ergonomic 設計

→ option A (state machine) が **設計思想と一貫**: P3 (OS = runtime engine、 LLM
= decision engine) を厳密適用すると、 discovery 後の state は OS 層で track
すべき。 batch 6 の構造的検討項目として優先。

## 後続 candidate

1. option A の設計検討 + PR 化 (batch 6 の主要 work)
2. attractor 発生確率の定量化 (= 同 scenario を 10 回回して fail rate 測定)
3. 強モデルでの reproduce 試行 (= G4 trigger 発火検討材料)

## 教訓

1. **attractor は 1 つの variant を塞いでも別位置で出現**: batch 2 → 3 → 5
   retest 2 で 3 回目の出現。 prompt rule に依存する戦略では完封できない
2. **対症療法サイクルからの脱出**: prompt rule を bullet 単位で増やすと
   memory `feedback_prompt_design.md` の bloat 警告と直結。 構造的解 (=
   OS 層 state machine) への pivot が必要
3. **B2-H1 fix の rule が今も prompt にあるのに発症**: rule の存在は必要
   条件だが十分条件ではない。 weak LLM の確率的挙動を前提にした **layered
   defense** (= prompt rule + OS gate の 2 段) が必要
4. **prediction 「fix 90% で動く」 が 2 batch 連続で外れた**: fix-verify batch
   の prediction は high bias、 batch 6 では「fix が再 attractor を生む可能性」
   を必ず外れ予測に含める
