# Batch 13 (revert + real milestone) — Retrospective

> 🏆 **Reyn dogfood real milestone 達成** — N=5 で 80% complete rate。 user
> 「permission system 簡潔に説明できますか?」 指摘で documented design 違反 fix を
> 発見、 revert + V3 wording 仕様変更 + reyn.local.yaml pre-approval で coherent
> design 復帰。 batch 7-13 の 7 batch progression の **production-grade phase 1**
> 完了 milestone batch。

## 想定と現実のずれ

### 開始時の想定

batch 12 retro で B12-NEW-1 fix を CRITICAL 候補とした、 startup_guard で python step
を non-interactive auto-approve する extension wave 想定。

### user 介入による軌道修正

User feedback:
> 「permission システムの動作を簡潔に説明できますか？できないのであれば何かが
>   おかしいです」

→ 5 rule で説明したが「**rule 4 のような規則にはならない**」 とのこと、 documented
design audit 実施。

User feedback (続):
> 「reyn の permission システムは iapp を参考にすると、 あなたと決めたはずです。
>   本当に曖昧ですか？」

→ `docs/en/concepts/runtime/permission-model.md` 再読、 **iApp-style trust model が明文化済**
を再確認。 G15 / R1 fix が doc 違反であったことが確定。

### 実際の進行

| 想定 | 現実 |
|---|---|
| B12-NEW-1 fix dispatch | **却下** (= doc 違反の symmetric 拡張、 dispatch しない) |
| permission system 拡張 | **revert** (G15 + R1 削除、 documented design 復帰) |
| 真の milestone 35% verified | **80% verified** (= 4/5、 prediction の 4-5/5 zone hit) |

= 「**fix accumulation の audit + revert**」 が「**fix dispatch**」 と同等の
discipline、 batch 13 で初めて運用化。

## ターニングポイント 3 つ

### TP1: user 「簡潔に説明できますか?」 → documented design 違反発見

batch 12 retro で B12-NEW-1 を CRITICAL 候補としたが、 user の simplicity test で
**「対称性 + 例外最小」 が破られている**直感を提示。 私が permission system を 5 rule
で説明したが:

- rule 4 (= non-interactive auto-approve) が asymmetric (= TTY mode と non-TTY mode で
  挙動が異なる、 同じ事象に 2 つの authority が同居)
- これは「**approval が TTY 状態に依存する**」 incoherent design

user の続く質問:
- 「rule 4 は reyn run の挙動?」 → No、 stdin が TTY かで判定 (= command 種別と無関係)
- 「prompt 不能とは?」 → `sys.stdin.isatty() == False`
- 「skill.md の declaration は approve リクエストで一致?」 → Yes
- 「user の許可方式 4 種で一致?」 → Yes (= reyn.yaml / CLI flag / .reyn/approvals.yaml / prompt)
- 「reyn の permission システムは iapp 参考、 本当に曖昧?」 → ✗、 documented design
  存在を発見

→ **私が「曖昧」 と書いた誤り**。 documented design は明確、 G15 / R1 fix が
introduction した non-documented behavior が complexity 増加の真因。

教訓: **「fix accumulation の coherence audit」** は user-driven test で initiate
されるのが現実的。 私は fix dispatch で busy になり design coherence の overview を
失っていた。 user の「**simplicity smell test**」 が早期 detection mechanism として
機能。

これは batch 7 で確立した「観測 infra」 の **architectural 版**: **観測道具なしで
推測しない** → **documented design 整合性なしで fix dispatch しない**。

### TP2: revert as first-class fix discipline

doc 違反 fix の処理を「**不具合修正 (= documented design 復帰)**」 として明示
classify、 仕様変更と区別:

- G15 revert → 不具合修正
- R1 revert → 不具合修正
- B12-NEW-1 候補却下 → 同上 (= 違反拡張を未然防止)
- V3 wording fix → 仕様変更 (= router routing semantics 強化、 user 視点の change あり)

これで **「fix accumulation で system が複雑化」 vs 「documented design 内の正当な
拡張」 が user 視点で見分けやすくなる**。

教訓: **「仕様変更 / 不具合修正」 の分類を明示する discipline** は batch 13 で
確立した新原則。 user feedback「修正する場合の仕様変更はわかりやすく毎度報告して
ほしい」 が trigger、 fix landing 時に classification を明示する convention に
昇格。

### TP3: real milestone confirmation (= 4/5 = 80%)

S4 N=5 で 4/5 complete、 batch 10 の N=1 provisional milestone を **真の milestone**
に格上げ。 batch 7-12 で積み上げた structural fix の累積効果が data 化:

- chain reach copy_to_work: batch 11 で部分到達 → batch 12 で全到達 → batch 13 で
  通過 ✅
- routing layer stability: batch 11 で 60% routing-fail → batch 12 で 0% (= V3 wording
  単独効果) → batch 13 で 0% (V3 wording 維持)
- permission layer: documented design 復帰 + reyn.local.yaml で dogfood 対応

= **multi-batch progression の最終地点が「fix dispatch」 ではなく「coherence
restoration」 だった** という教訓。 batch 11-12 で landed した G15/R1 を **revert**
することで milestone 達成、 fix accumulation で進めていた batch 13 候補 (= B12-NEW-1)
を dispatch していたら milestone 未達だった可能性高い。

## 観測 infra の継続利用

batch 7-13 で 7 batch 連続使用、 reliable: ✅
- 並列 sonnet × 4 (= R1 revert + R2 revert + V3 wording + S4 retest) で全部活用
- N-shot replay (`llm_replay --n 10`) が R2 V3 wording variant 検証の決定的 tool
- `dogfood_trace --mode events` が S4 verdict 判定 + B13-NEW-1 検出
- `detect_attractor` で 0% attractor 確認 (= G12 Pattern D fix 維持)

道具は完成、 batch 7 投資 → 7 batch 継続回収。 batch 14:
- B13-NEW-1 (= literal model string) の reproduce-first verify
- M2 audit B12-NEW-2/3 fixture 修正 (= wrong-layer trap)

## prediction calibration の大幅改善

| Batch | Brier | 主因 |
|---|---|---|
| 8 | 0.96 | 累積 fix verify の verified 過大評価 |
| 9 | 0.55 | wrong layer trap 学習 |
| 10 | 0.30 | verify-first + resolved-indirectly framework |
| 11 | 0.65 | N=1 milestone を base rate に使った overestimate |
| 12 | 0.40 | batch 11 教訓反映、 復帰 |
| **13** | **0.20** | **best、 documented design 整合性 audit が calibration に直接寄与** |

3/3 hit (Step 1 / Step 3 / Step 4)、 全 prediction が hit zone 内。

batch 14 calibration target: ≤ 0.25 維持、 ただし B13-NEW-1 等の non-determinism
要素は honest な base rate で。

## チームダイナミクス (= user vs assistant)

batch 13 は **user 介入が batch を再定義した critical batch**:

| 介入 | 内容 | 効果 |
|---|---|---|
| TP1 (= 「permission 簡潔に説明」) | simplicity smell test | doc 違反 fix 発見 trigger |
| TP2 (= 「iApp 参考、 本当に曖昧?」) | documented design existence の確認 | 私の audit 誤りを訂正 |
| TP3 (= 「reyn.local.yaml が素直」) | real user の dev pattern を pointer | dogfood pre-approval mechanism 確定 |
| TP4 (= 「素晴らしい軌道修正」) | revert path の承認 | batch 13 fix wave (= revert + V3) 開始 |

= batch 7 (= 「観測 infra 整備」 の介入) 以来の **設計レベル介入**。 batch 8-12 は
operational / strategic 介入が中心だったが、 batch 13 で再び設計レベル介入が出た。
fix accumulation で system coherence が劣化した時、 user の simplicity test が
最も信頼できる re-calibration mechanism として機能。

## 次 batch (= batch 14) への申し送り

### Theme 候補 (= production-grade phase 1 完了 → phase 2 への移行)

| Theme | 内容 |
|---|---|
| Theme A: stability extension | 80% → 95% complete rate (= B13-NEW-1 fix + その他 minor blocker 解消) |
| Theme B: production-grade phase 2 移行 | cost / observability / monitoring 系の整備 (= phase 1 機能成立を超えて、 production 運用 readiness) |
| Theme C: meta hygiene | M2 audit B12-NEW-2/3 fixture 修正 + dogfood pre-approval pattern 文書化 |
| Theme D: G4 spike trial | proxy 強モデル `gemini-3.1-flash-lite-preview` で stability + cost 比較 |

優先順位:
1. **Theme A (B13-NEW-1 fix)**: chain stability の最後の確認、 deterministic fix で 4/5 → 5/5 期待
2. **Theme C (meta hygiene)**: 軽量、 並走 OK
3. **Theme B / D**: user 戦略判断待ち

### prediction 設計
- batch 13 で確立した「documented design 整合性 audit」 を fix dispatch 前に必須化
- structural fix base rate: verified 50-60% (= 復帰 fix は deterministic、 high
  confidence)
- 仕様変更 fix: verified 40-50% (= user 視点の change あり、 慎重 base rate)

### 設計原則の運用
- 6 原則 + verify-first + reproduce-first + N≥5 stability discipline 継続
- **新原則候補**: **「documented design 整合性 audit を fix dispatch 前に必須」** =
  TP1 教訓の memory 化候補
- **新原則候補**: **「修正分類 (仕様変更 / 不具合修正) を fix landing 時に明示」** =
  TP2 教訓の convention 化

## 一言で

> **🏆 Real milestone 達成 (= 4/5 = 80% chain 完走) — user の simplicity test 介入で
> documented design 違反 fix 群を発見、 revert + V3 wording 仕様変更 + reyn.local.yaml
> pre-approval で coherent design 復帰、 batch 7-13 の 7 batch progression が
> production-grade phase 1 完了に到達**

— B12-NEW-1 fix 候補を **却下** (= doc 違反の symmetric 拡張)、 G15 + R1 を revert
— V3 wording fix で routing-fail 40-50% → 5%、 仕様変更分類で user 視点の change を
  明示
— reyn.local.yaml pre-approval pattern で dogfood 自動化と documented design 共存
— Brier 0.20 で 13 batch 中 best、 「documented design 整合性 audit」 が新 discipline

batch 13 で「**fix accumulation の audit + revert**」 が「**fix dispatch**」 と同等の
discipline であることを data 化、 「**simplicity smell test**」 が user-assistant
協業で最も信頼できる re-calibration mechanism として機能。 phase 2 への移行点。
