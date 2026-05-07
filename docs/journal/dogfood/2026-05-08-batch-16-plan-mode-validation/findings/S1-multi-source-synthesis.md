# S1: Multi-Source Synthesis — Batch 16 Findings

| Field | Value |
|---|---|
| Date | 2026-05-08 |
| main HEAD | `4912457` |
| Scenario | S1 — router が 2 読み込み + 1 合成の 3-step plan を自律的に立案するか |
| Agent | `b16_s1` |
| Sample size | N=5 |
| **Verdict breakdown** | **refuted: 5 / verified: 0 / inconclusive: 0 / blocked: 0** |

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 40% (2/5) | 0% (0/5) |
| inconclusive | 20% (1/5) | 0% (0/5) |
| refuted | 30% (1.5/5) | 100% (5/5) |
| blocked | 10% (0.5/5) | 0% (0/5) |
| plan invoked | 70% | 0% |
| total elapsed | — | 12.6s (avg 2.5s/run) |
| est. S1 cost | ~$0.005 | ~$0.005 (5 calls × 1 call/run) |

予測 Brier: E[B] = 0.40×(1-1)²+0.20×(0-1)²+0.30×(1-0)²+0.10×(0-0)² = 0 + 0.20 + 0.30 + 0 = **0.50** (= 最悪値方向)  
実測 Brier: B = (0-1)²×1 = **1.00** (= 全外れ、 refuted が 5/5 で予測と完全に逆)

Brier delta: **+0.50** (= 予測精度 大幅 miss → calibration 要修正)

---

## 2. Per-Run Details

| Run | Verdict | plan_invoked | Steps | Elapsed | reply_len | Note |
|---|---|---|---|---|---|---|
| 1 | refuted | False | 0 | 4.3s | 905 | Cold start。 training-data から 3 段落生成 |
| 2 | refuted | False | 0 | 1.2s | 171 | history bleed (B16-S1-1)。 「既に回答済み」 英語返答 |
| 3 | refuted | False | 0 | 3.6s | 864 | history 蓄積後の 2 回目 cold-like run |
| 4 | refuted | False | 0 | 1.0s | 163 | history bleed 再度。 「既に回答済み」 英語返答 |
| 5 | refuted | False | 0 | 2.5s | 698 | history 蓄積後の 3 回目 run |

プロンプト (全 run 共通):
```
README.md と CLAUDE.md を読んで、両者を比較する 3 段落の文章を書いて
```

---

## 3. What Happened

### 5 run 全て: plan tool 未 invoke、 直接 text-reply

router LLM は全 5 run で `plan` tool を invoke せず、 training-data knowledge から
README.md / CLAUDE.md の内容を直接合成して text 返答した。 smoke run 観測 (single run、
default agent) と完全に一致。 file_read も呼ばれていない (WAL に `file_read` event なし)。

### history bleed バグ (B16-S1-1)

Run 2 と Run 4 で顕著な劣化を確認: `clean_state("b16_s1")` が `state/` + `events/` を
wipe するが、 `history.jsonl` (= `agent_dir` root に存在) は保持される。
結果として N=5 が独立した fresh run ではなく **単一の growing session** として動作した。

timeline:
- seq 1-2: 初期 hello (= profile.yaml 存在済みの既存 turn)
- seq 3-4: run 1 の prompt → 905 字回答
- seq 5-6: run 2 の同プロンプト → 「already answered」 (= history 参照)
- seq 7-8: run 3 → 864 字回答 (history に run 1 + 2 が蓄積)
- seq 9-10: run 4 → 「already answered」
- seq 11-12: run 5 → 698 字回答

Run 2 / 4 は独立した run として無効 — N=5 中実質 3 run が fresh start。
ただし plan invocation は全 run で 0 であるため verdict distribution への影響は小。

### reply content 観察

3 つの substantive reply (run 1, 3, 5) はいずれも Reyn アーキテクチャの説明 +
CLAUDE.md の P1-P8 の概要を含む 3 段落構成。 実際のファイルを読まずに training-data
knowledge から合成しているにもかかわらず、 内容は概ね正確 (= Gemini 2.5 flash lite の
知識の範囲内に CLAUDE.md 類似文書が存在する可能性)。

---

## 4. What It Means

### G1 リスクが全面的に materialise

prelude のリスクノート: 「batch 1-14 で router LLM の text-reply attractor を繰り返し観測。
plan tool は新規追加なので attractor の degree は未知。」 → 実測: attractor が **100%**。
plan tool は real LLM 環境では一切 invoke されなかった。

これは 2 つの可能性を示す:

1. **Tool definition が router LLM に届いていない** (= b16_s1 agent の profile.yaml に
   plan tool が registered されていない、 または system prompt に tool catalog が注入されていない)
2. **Router LLM が plan tool を認識しているが invoke の閾値を超えていない**
   (= prompt が ambiguous または タスクが「十分に complex」と判定されていない)

Run 1 の elapsed (4.3s) は tool-use なし直接回答としては自然な速度、 Run 2/4 の 1s 台は
history bleed での即答。 Tool 呼び出し開始→完了の latency pattern も見られない。

現段階では判定 1 (tool not registered) が有力 — b16_s1 の profile.yaml は `role: ''`
のみで tool binding 設定なし。

### Calibration への示唆

予測 40% verified → 実測 0%: refuted rate を保守的 (30%) と見積もったが、
実際の attractor strength は 100% だった。 batch 1-14 での text-reply attractor 観測を
「plan tool なし環境」の現象として過小評価した。 plan tool が有効な agent でも
attractor が同等であることを確認した。

---

## 5. New Bugs

### [HIGH] B16-S1-1: `clean_state` が `history.jsonl` を wipe しない

| 項目 | 詳細 |
|---|---|
| ID | B16-S1-1 |
| 重要度 | HIGH (= N=5 runs の独立性が崩れる、 dogfood の観測精度に直結) |
| 現象 | `clean_state(agent_name)` が `state/` + `events/` を削除するが `history.jsonl` は agent_dir root に残り、 次の run に引き継がれる |
| 証拠 | Run 2 の history.jsonl (seq 1-6): run 1 の turn が seq 3-4 として残存、 run 2 の prompt が seq 5-6 として追記 |
| 影響 | Run 2 / 4 が degenerate reply (「already answered」) となり独立した fresh run でない。 N=5 が実質 3 run になる |
| 修正候補 | `driver.py` の `clean_state` に `history.jsonl` の wipe を追加 (= `agent_dir / "history.jsonl"`) |
| scope | driver.py のみ — OS コード変更不要 |

### [MED] B16-S1-2: plan tool が real LLM で invoke されない (G1 unverified)

| 項目 | 詳細 |
|---|---|
| ID | B16-S1-2 |
| 重要度 | MED (= batch 16 の核心 G1 が観測不能) |
| 現象 | plan tool が 5/5 run で invoke されず、 router が直接 text-reply attractor に落ちる |
| 仮説 | b16_s1 profile.yaml に plan tool binding がない、 または plan tool が router の tool catalog に注入されていない |
| 次 action | b16_s1 profile.yaml の tool 設定確認 + plan tool registration ログを確認して仮説 1 / 2 を分離 |
| scope | 観測確認が先、 fix は仮説確定後 |

---

## 6. Calibration Delta

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 40% | 0/5 (0%) | (0.4-0)² = 0.16 |
| inconclusive 20% | 0/5 (0%) | (0.2-0)² = 0.04 |
| refuted 30% | 5/5 (100%) | (0.3-1.0)² = 0.49 |
| blocked 10% | 0/5 (0%) | (0.1-0)² = 0.01 |
| **Brier score** | — | **0.70** (= 4 class 平均: 0.175) |

batch 14 最終 Brier 0.18 から大幅悪化: refuted 率の保守的過小評価が原因。
「plan tool が新規追加だから attractor は未知」ではなく「plan tool が未 invoke なら
smoke run と同じ text-reply attractor が 100%」という prior を更新する必要あり。

次回 S1 再実施 (= B16-S1-1 + B16-S1-2 修正後) の予測補正:
- verified: 20% (下方修正)
- inconclusive: 15%
- refuted: 55% (上方修正)
- blocked: 10%
