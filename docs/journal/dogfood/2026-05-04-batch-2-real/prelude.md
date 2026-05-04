# Batch 2 (Real) — Prelude

> 練習 batch (= batch 1) で発覚した 11 件 (HIGH 6 / MED 2 / LOW 2) のうち、
> HIGH 6 + MED 2 = **8 件 fixed**。 Tier 2 invariant は 660 passed。 だが
> **e2e 実 LLM での改善確認は未実施**。 batch 2 はその確認 + 新領域開拓の
> 本格 batch。

## batch 1 → 2 の間で何が起きたか

batch 1 完了 (2026-05-04 早朝) → A5 wave (HIGH bug fix 4 PR) → Q2 wave
(言語 fallback 廃止) と続いた。 commit を時系列で:

| Commit | 内容 | finding 対応 |
|---|---|---|
| `f5b3281` | InterventionRegistry attach 経路 fix | F1 |
| `2e595b9` / `98d849c` / `e8da4f7` | journal docs (prelude / scenarios / findings / retrospective + summary table + A4 review 結果) | (docs) |
| `e59cead` | router F3+F9 / MCP F10 / i18n F11+F8 を 1 commit | F3 / F8 / F9 / F10 / F11 |
| `9e8126c` | multi-agent F5 (delegate dedupe) + F6+F7 (no-reply marker) | F5 / F6 / F7 |
| `61dc719` | findings status 更新 | (docs) |
| `42d5bc1` | findings.md → per-finding split | (docs cost 削減) |
| `651d7f3` | Q2: output_language Optional[str] (= 言語 fallback 廃止) | F11 follow-up |

**未対応 (Wave B coverage audit へ)**: F2 (reyn.local.yaml docstring) +
F4 (cost 永遠の 0)。 LOW 2 件、 dogfood の主観点 (= chat 会話 UX) と独立。

## 期待 / 不安

私 (assistant) の事前仮説 — batch 2 で:

- **regression net (3 件 = S1 / S2 / S3)**: 全て pass する **ハズ**。 ハズだが
  Tier 2 の green は単体動作証明、 e2e で本当に LLM が新 prompt rules を
  honor するかは別問題。 特に F3+F9 は弱モデル (gemini-2.5-flash-lite) の
  attractor 依存性があり、 確信できない。
- **Q2 retest (1 件 = S4)**: config 未設定で英語 chat → 英語応答、 同 config
  で日本語 chat → 日本語応答。 これは prompt から directive を抜く change
  なので動くハズ。
- **新領域 (4 件 = S5 〜 S8)**: ask_user / nested skill / memory / postprocessor
  系。 batch 1 で skill が起動しないので touch すらできなかった領域。 ここで
  「あれ?」 が出る可能性は 50%+ と予想。

batch 1 の事前 prediction は当たり 0/4 だった (現実が予想以上に深刻、
方向への外し方)。 batch 2 でも同方向で外す可能性: 「regression net 全 pass」
を期待しているが、 1-2 件再発しても驚かない。

## 進め方

batch 1 と同じ 5 step:

1. **A1**: 私 (assistant) が scenario リスト初版を作成 (= この `scenarios.md`)
2. **A2**: user がレビュー → scenario リスト v2
3. **A3**: 私が実 LLM 経由で実行 (Sonnet sub-agent 委託 OK)、
   findings.md + per-finding files 起草
4. **A4**: user が私の感覚との差を共有
5. **A5**: HIGH/MED/LOW 分類 → fix wave or Wave B 送り

## 何が起きたら成功と呼ぶか

- **regression 0**: batch 1 HIGH bug が再発しない (= S1 / S2 / S3 全 pass)
- **新 finding が出ても対処可能**: 新領域で発見された bug は HIGH でも
  「 batch 1 ほどの cascade 連鎖ではない」 ことが確認できれば本 batch の
  結論として「 OS は core が安定し始めた」 と言える
- **MED / LOW finding は Wave B へ**: F2 / F4 と合流して coverage audit で
  整理

## 関連 doc

- [batch 1 findings.md](../2026-05-04-batch-1-practice/findings.md) — 前 batch
  の summary table + narrative
- [retrospective.md](../2026-05-04-batch-1-practice/retrospective.md) — batch
  1 の learnings (= バッチサイズの教訓 / user 感覚との差 0/11 等)
