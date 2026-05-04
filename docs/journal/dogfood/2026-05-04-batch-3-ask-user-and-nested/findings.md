# Batch 3 (ask-user-and-nested) — Findings

> regression net: B2-H1/H2 fix の効果を multi-agent S1 で検証 → **B2-H2 fix は機能 ✅**
> (peer 失敗が user に届く)、 **B2-H1 fix は variant attractor で部分回避** ✗
> (`describe_skill → stop` は消えたが `list_skills → stop` 新変種が specialist 側に発生)。
> ask_user IR op 経路は **依然 dark** — skill 起動段階で attractor に阻まれて IR op
> まで到達しない。

このファイルは index です。 各 finding の詳細は per-scenario observation に
分割済み (`findings/B3-Sx-observation.md`)。

## 概要

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [B3-H1](findings/B3-S1-observation.md) | HIGH | specialist の RouterLoop が `list_skills` 後に空 reply で停止 — B2-H1 fix の variant attractor | open |
| [B3-M1](findings/B3-S1-observation.md) | MED | scenarios.md の S1 に specialist agent 作成手順が抜け、 setup 段階で fail (= scenario 設計の不備) | open |
| [B3-M2](findings/B3-S2-observation.md) | MED | router が `read_local_files` 明示 + 4 turn 全てで `list_skills` / `invoke_skill` 呼ばず direct reply (B2-M1 family の再発) | open |
| [B3-M3](findings/B3-S5-observation.md) | MED | `list_skills` で matching skill 発見後に `invoke_skill` skip して direct reply の attractor (B3-H1 と同 family、 default 側) | open |
| [B3-INFO-A](findings/B3-S3-observation.md) | INFO | `eval_builder` が `run_skill` 未使用設計 → S3 setup 不可。 stdlib で `run_skill` を使うのは `eval` / `skill_improver` / `judge_phase` のみ | batch 4 再設計 |
| [B3-INFO-B](findings/B3-S4-observation.md) | INFO | B2-M4 (narrator 「完了しました」 のみ) は **自然改善** で 1 turn に内容が届く | resolved |
| [B3-L1](findings/B3-S1-observation.md) | LOW | `:cost` slash の pexpect capture 失敗 — CUI mode で `/quit` のみ slash 認識 | open |
| [B3-L2](findings/B3-S4-observation.md) | LOW | router が narrator 後に追加 LLM 呼び出し (chain_id 同一) で詳細を再送信 — UX 冗長 | open |
| [B3-L3](findings/B3-S4-observation.md) | LOW | B2-M3 (MCP teardown anyio cancel scope RuntimeError) 再現 | open |

**HIGH 1 件 / MED 3 件 / INFO 2 件 / LOW 3 件** (合計 9)。

**B2 fix 効果検証**:

- **B2-H2** (`_no_reply_marker` silent absorption): ✅ S1 で `peer_reply_failed_surfaced` 発火、 user に「specialist から処理結果が得られませんでした」 が届いた
- **B2-H1** (specialist `describe_skill → stop`): ✗ describe 経由は防げたが、 `list_skills → stop` という別の attractor で同じ「invoke しない」 結末に到達 (B3-H1)
- **B2-H3** (MCP permissions): ✅ S4 で MCP config + permissions 経由で read_local_files 動作
- **B2-M1** (skill 名 hallucination): partial — `general.summarize` 発明はなし、 ただし invoke skip の attractor は残る (B3-M3)
- **B2-M4** (narrator 2-turn): ✅ S4 で 1 turn で内容届く、 自然改善で resolved
- **B2-M3** (MCP teardown error): ✗ S4 で再現 (B3-L3)

---

## ハイライト narrative

### 「list_skills → 何もしない」 attractor family — B3-H1 + B3-M3

batch 2 で B2-H1 fix を入れた時、 `describe_skill → silent stop` を防ぐ
ルールを `router_system_prompt.py` に追加した。 batch 3 では同じ attractor の
**variant** が 2 通り観測された:

1. **specialist 側 (S1、 B3-H1 HIGH)**: `list_skills("")` →
   `list_skills("general")` まで呼んだ後、 invoke_skill 呼ばず空 reply 送信。
   B2-H1 ルール「describe 後 invoke or 説明」 は describe を経由しないこの
   経路を gate しなかった
2. **default 側 (S5、 B3-M3 MED)**: `text_summarizer` が catalog に存在する
   ことを確認した後、 invoke_skill 呼ばず direct reply。 LLM が「自分で答えれば
   よい」 と判断する attractor

両者とも構造的に「**list_skills の結果を活用しない**」 同じ attractor。
fix 候補 = router_system_prompt.py に「`list_skills` で matching skill を
発見したら、 `describe_skill` または `invoke_skill` を呼ぶこと。 直接返答禁止」
を追加。 B2-H1 ルールの拡張として 1 行追加で対応可能と推定。

### ask_user IR op は依然 dark — S2 の setup 不備

S2 は ask_user e2e の初観測を狙ったが、 router が **list_skills も invoke_skill も
呼ばず** に 4 turn 全て direct reply を返した (= B3-M2)。 skill 起動しないと
IR op まで届かないので、 ask_user 経路は本 batch でも観測不能。

これは B3-H1 / B3-M3 の attractor family と同根 — router_system_prompt の
強化で skill 起動の確実性が上がれば自然と観測機会も生まれる。 batch 4 で
B3-H1 fix 後に再挑戦する方針。

### nested skill の setup 問題 — S3

`eval_builder` skill は `analyze_skill → write_eval` の 2-phase 完結で、
内部で `run_skill` を呼ばない設計と判明。 stdlib で `run_skill` を使うのは:

- `eval` の `run_target` phase (target skill を呼ぶ)
- `skill_improver` の `run_and_eval` phase (`eval` + `eval_builder` を呼ぶ)
- `judge_phase` (eval の preprocessor から呼ばれる被呼び出し側)

**3-4 階層 nested chain** を一度に観測したい場合は `skill_improver` を
入り口にする scenario が batch 4 で適切。

### narrator 自然改善 — B2-M4 → resolved (S4)

B2-M4 は「narrator が `完了しました` のみで skill 出力を含まない」
と判定していた MED bug。 batch 3 S4 で同じ手順 (`read_local_files で
README.md 読んで 1 段落で説明`) を実行したところ:

> README.md を読み込みました。 Reyn プロジェクトは、 予測可能性、 監査可能性、
> および自律性よりも制約を優先する LLM ワークフロー OS であると説明されています。

**1 turn で具体的内容が届いた**。 fix 無しで自然改善 (= LLM 出力の
ばらつき範囲だった可能性、 もしくは batch 2 観測時の prompt context が
たまたま narrator を generic に振っていた可能性)。 resolved として close。

ただし副次的に B3-L2 (router が narrator 後に追加 LLM 呼び出し) を発見。
narrator の応答後に同 chain_id で seq=3 の LLM call が走り「詳細説明」 を
別 message として送信していた。 重複情報で UX が冗長、 機能影響は無いが
LOW として open。

---

## 事前 prediction の精度

batch 3 開始前の事前仮説 5 件:

- **S1 「70% でカレー届く」**: 大外れ (= 30%、 H1 fix の variant attractor で停止 → B3-H1 発見)
- **S2 「40% で ask_user 観測」**: 当たり (= router が skill 呼ばず外れ予測通り)
- **S3 「35% で nested chain」**: 当たり (= eval_builder が run_skill 未使用、 setup 問題で外れ予測通り)
- **S4 「45% で 1 turn 成功」**: 当たり (= 1 turn で内容届いた、 B2-M4 自然改善)
- **S5 「30% で hallucination 解消」**: partial (= 発明はなし、 attractor 別形で残る)

精度: **方向当たり 4/5**。 batch 2 の 3/5 から改善。 唯一 S1 のみ大外れ
だが、 これは「外れ予測 = chain 接続で新問題」 まで意識していたものの、
attractor の variant 化を予測しきれなかった。 「fix が効いた領域は別経路で
再発する」 という pattern を batch 4 の prediction で意識的に組み込む。

---

## 結論

> **B2 wave の HIGH 3 件のうち H2/H3 は完全動作確認、 H1 は variant attractor
> として再発し B3-H1 [HIGH] に再分類。 B2-M4 は自然改善で resolved。
> ask_user e2e は依然 dark。 narrator 品質は改善方向だが冗長 LLM 呼び出しの
> 新 LOW を発見。**

router_system_prompt.py の attractor 対策強化が batch 4 の主軸に。
B3-H1 + B3-M3 を 1 個の prompt rule で吸収する fix wave + その後の e2e
再観測 (= 真の curry recipe + ask_user の発火) が次のアクション。

---

## 次のアクション

1. **B3-H1 fix** — `router_system_prompt.py` に「`list_skills` で matching
   skill 発見後は `describe_skill` か `invoke_skill` を呼ぶ。 直接返答禁止」
   ルール追加。 B2-H1 fix の拡張として 1-2 行で対応可能。 specialist + default
   両方で効果を持つ。
2. **B3-M3 解消の確認** — 上記 fix で同時 close できる見込み (同 family)。
3. **scenarios.md 修正 (B3-M1)** — S1 に specialist agent 作成手順を追記。
4. **batch 4 設計** — B3-H1 fix 後に S1 + S2 を再実行、 加えて
   `skill_improver` を入り口にした 3-4 階層 nested chain scenario を新規追加。
5. **B3-L1 解消** — pexpect が `:cost` を slash として認識できるよう CUI mode
   の slash dispatch を確認、 必要なら `/cost` alias 追加 (low priority)。
6. **B2-M3 再確認** — S4 で再現 (B3-L3)、 long session の anyio teardown
   経路を別 PR で検討 (R-Dx 系で tracked)。
