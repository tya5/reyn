# B3-M3 [MED]: default 側でも list_skills 後 invoke skip — B3-H1 と同 family

> 一行で: specialist だけでなく default でも、 `list_skills` で matching skill
> を見つけてから `invoke_skill` を呼ばずに direct reply で済ませる attractor
> が出た。 B3-H1 の default-side mirror。

| Field | Value |
|---|---|
| Severity | MED |
| Status | **fixed** at `48676ad` (B3-H1 と同 commit) → `ca116f3` で final |
| Scenario | S5 (skill 名 hallucination 確認) |
| Found | 2026-05-04 |
| Raw observation | [B3-S5-observation.md](B3-S5-observation.md) |

---

## 観測

S5 で user input `次の英文を 3 つの bullet point に要約して: ...` (= B2-M1
再現用 input) を default に送信。 router の tool_call sequence:

```
list_skills("")              → 10 skills returned
list_skills("text_summarizer") → matching skill found in catalog
agent_message_sent           ← invoke_skill 呼ばず、 LLM が直接 3 bullet 要約 reply
```

reply 内容は正しい (= 日本語で 3 bullet)、 ただし **skill ecosystem を
bypass**。 `text_summarizer` は catalog に居て LLM はそれを認識したが、
「自分で要約できる」 と判断して invoke を skip した。

## つまり何が起きたか

これは B3-H1 (specialist の `list_skills → 停止`) と **同じ attractor の
default-side mirror**。 違いは:

- B3-H1 = list 後に何もせず空 reply (= specialist で観測)
- B3-M3 = list 後に invoke skip して LLM が代わりに reply (= default で観測)

両者とも構造的に「**`list_skills` の結果を活用しない**」 同じ family。 LLM は
matching skill の存在を知っていて、 その上で「invoke しない」 判断を取って
いる点が共通。

B2-M1 の元 finding (= `general.summarize` を hallucinate) との比較:

- B2-M1: skill 名を **発明** (= hallucination)
- B3-M3: skill 名は **正しく認識** したが **bypass**

→ batch 3 fix wave で B2-M1 hallucination は **partial 解消** (= 発明はなくなった)、
ただし B3-M3 のような **invoke skip** が新変種として出現。 「fix した穴の脇から
attractor が漏れる」 pattern の典型。

## 影響

- skill ecosystem (= phase 構造、 schema 検証、 preprocessor / postprocessor) を
  user が体感できない、 reply は正しいので user は気づかない
- catalog にある skill が呼ばれない = skill 開発の motivation 低下
- skill 経由の cost / event 記録が残らない (= audit 観点で記録漏れ)

## 修正

### 第 1 弾 (`48676ad`)

B3-H1 と同 commit。 `router_system_prompt.py` に「`list_skills` で matching
skill を発見したら、 `describe_skill` か `invoke_skill` を呼ぶ。 直接返答禁止」
ルールを追加。 specialist と default 両方の system prompt は共通なので 1
ルール追加で両方の attractor を gate。

### 第 2 弾 (`ca116f3`)

B5-H1 (= B3-H1 + B3-M3 の rule が consolidation で signal 弱化した regression)
の re-balance fix で final 化。 5 個別 bullet × 各 1 MUST 形式に戻し、 wording
のみ dedup。 B3-M3 の効果も同時に回復。

## 後続 candidate

- 強モデル (= claude-sonnet / gemini-2.5-pro) で同 scenario を回し、
  default-side の invoke skip attractor が消えるか確認 (= G4 trigger)
- 「`list_skills` で見つかったのに何で呼ばないか」 を LLM に問う形式の dogfood
  を batch 6 で組み込む (= 黒箱の中身を text で吐かせる)

## 教訓

1. **specialist と default で同じ attractor が観測される**: 同 system prompt
   を使う以上当然だが、 finding 起こすときは「片方で出たら他方も verify」 を
   default 動作にすべき
2. **B2-M1 の hallucination → B3-M3 の bypass の進化**: fix の効果は
   「hallucination 消失」 に留まり、 「skill ecosystem を活用」 にまで
   到達するには別 layer の対処が必要
3. **rule lifecycle の追跡**: B3-H1 + M3 fix `48676ad` → consolidation
   `e90c0f2` で破壊 → `ca116f3` で再構築、 という rule の生死を doc から
   辿れる形で記録 (= 各 finding の Status field に lifecycle を時系列で書く)
