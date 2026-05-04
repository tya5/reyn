# Batch 6 (non-attractor focus) — Prelude

> attractor 系 (= G12) は触らず、 真の解 = 強モデル trigger 評価 (Wave 3) に
> 委ねる。 batch 6 では **非 attractor 系の課題** に focus し、 G2 post-fix
> verify + G5 ask_user 観測 + MED 3 件の現状 data 収集を狙う。

---

## 前夜 — main HEAD `6c8542c` 時点の reyn 状態

### 完了済 (= batch 1-5 + catch-up wave)

- **batch 1-5 dogfood 完走** (= 5 batch / 9+ scenario / 35+ finding)
- **HIGH bug**: F1-F11 / B2-H1〜H3 / B3-H1 / B4-H1〜H2 / B5-H1〜H2 / B5R2-H1 を
  処理 (= B5R2-H1 は G12 移管)
- **giveup tracker 12 案件 management 化**: 2 件 resolved (G2 / G9)、
  10 件 active (= G1 / G3〜G8 / G10 / G11 / G12)
- **ツール化**: `dogfood_trace.py` + `rekey_fixtures.py` (= operational
  efficiency 蓄積)
- **memory 化教訓**: `feedback_prompt_design.md` (bloat / consolidation 両方
  危険) + `feedback_deterministic_split.md` (G2 で実証された決定論分離思想)
- **batch 3-5 docs catch-up**: retrospective.md ×3 + per-finding 5 要素
  ×10 + findings.md narrative 復元
- **Wave 1 (G12 追加)** landed at `6c8542c`

main HEAD: `6c8542c`、 736 passed / 2 xfailed。

### 重要な方針転換 (= 当 batch の前提)

batch 5 retest 2 で attractor 3 度目発生を確認後、 当初は OS 層 state
machine (= PR-state-gate) を提案したが、 user feedback で:

> こう言うのを想定してギブアップリスト作ってるんですが、 ギブアップ
> リストに移動ではだめな案件? weak model でも対処すべき案件?

を受けて **撤回**、 G12 化 + G4 trigger 評価へ pivot。 詳細は plan file の
「NEXT WAVE PROPOSAL」 section と memory `feedback_deterministic_split.md` を
参照。

batch 6 は **attractor 系を意図的に触らない**。 attractor 発生時は G12
monitoring data として記録、 fix dispatch しない。

---

## 事前仮説 (5 scenario の prediction、 全て internal/user metric 分離記録)

### 共通

- **internal metric**: `dogfood_trace --mode summary` で観測される events /
  WAL エントリの sequence
- **user metric**: CUI に映る reply の内容 / actionable error の有無 / 体感

両者の hit/miss を分けて記録する。 「invoke 到達 ✅」 と「内容届く」 の乖離が
batch 4 retro で identified された問題、 その train を batch 6 では prediction
段階から組み込む。

### Scenario 別 prediction (= 詳細は scenarios.md)

| ID | Internal prediction | User prediction | 外れ予測 |
|---|---|---|---|
| S1 (G2 post-fix retest) | 90% workspace dir 作成 ✅ | 70% improvement 案が user に届く | eval cascade で別 attractor (= LLM 判断ばらつき)、 G12 family 該当時は記録のみ |
| S2 (G5 ask_user trial) | 30% IR op 発火 (weak LLM) | 20% user に prompt 届く | router が再び pre-skill clarification or G12 attractor で skill 起動段階で停止 |
| S3 (B5-M1 観測) | 50% 並列 invoke 再現 | n/a (= 観測のみ) | LLM judgment ばらつきで並列起動しないケースあり |
| S4 (B2-M2 観測) | 70% tool_failed 後 fallback path 経路通過 | 50% 英語 reply が出る | tool_failed scenario を意図的に踏ませる難しさ |
| S5 (B4-M1 観測) | 80% eval.md path search で 4 回 failed read 観測 | n/a (= 観測のみ) | skill_improver chain が attractor 系で途中で止まる場合あり |

---

## 観測体制

### 必須 tool (= cost 削減 + 観測整合性)

```bash
# 各 scenario 完了後
python scripts/dogfood_trace.py --root .reyn --mode summary
python scripts/dogfood_trace.py --root .reyn --mode chain
python scripts/dogfood_trace.py --root .reyn --mode cost
```

### batch 5 で issue ありの infrastructure 改善

- **`/quit` 前に sleep 必須**: piped input で `reyn chat` を回す場合、 非同期
  peer agent の routing 完了を待つ必要あり (= batch 5 retest 2 で判明)
- pexpect timing: timeout 60s/turn を保つ、 long timeout は不要 (= G5 で peer
  delay が大きい時は別途調整)

### G12 attractor 発生時の handling

- attractor 観測時は **fix dispatch しない**
- `giveup-tracker.md` の G12 section に observation data 追記:
  - 発生 scenario
  - attractor の variant (= describe→stop / list→stop / list→bypass)
  - LLM model + temperature
  - context length
- Wave 3 G4 spike の比較 baseline data として活用

---

## 工程と user 介在

```
A1 (= 本 prelude + scenarios.md draft)  — sequential、 私が書く
   ↓
A2 review                               — user 介在 ← 必須、 skip 禁止
   ↓
A3 execution                            — sonnet 並列 OK (= worktree 隔離)
   ↓
A4 finding aggregation + 感覚 review    — user 介在 ← 必須、 skip 禁止
   ↓
A5 fix wave (= 非 attractor のみ)       — sequential、 並列禁止
   ↓
retrospective                           — batch 1 quality 維持
```

A2 / A4 で user review を **必ず挟む**。 batch 3-5 で skip した結果の
cross-batch interference を予防する process 改善。

---

## 関連 docs

- [scenarios.md](scenarios.md) — 5 scenario の詳細
- [giveup-tracker.md G12](../giveup-tracker.md) — attractor variant family
- [batch 5 retest 2 retrospective (= batch 5 fix-verify retro 内)](../2026-05-04-batch-5-fix-verify/retrospective.md) — 直前 batch の教訓
- memory `feedback_deterministic_split.md` — G2 で実証された決定論分離思想、
  本 batch S1 の前提
- memory `feedback_prompt_design.md` — bloat / consolidation の警告、 本 batch
  で attractor 観測時の指針
