# B6-S3 Observation: router parallel skill_improver invocation

**Date**: 2026-05-04  
**Scenario**: S3 (B5-M1 再現観測)  
**Input wording**: `skill_improver を使って direct_llm を review して`  
**LLM**: gemini-2.5-flash-lite via LiteLLM proxy at localhost:4000  
**Execution**: 2 pexpect sessions (Run 1 killed early; Run 2 partial completion)

---

## 並列 invoke 数

`ls .reyn/events/agents/default/skill_runs/` で確認した skill run ファイル数:

### Run 1 (T14:25:29–14:25:32)
```
2026-05-04T142529_skill_improver.jsonl     # 38 events
2026-05-04T142530_skill_improver.jsonl     # 80 events  (+64ms)
2026-05-04T142530_skill_improver_1.jsonl   # 71 events  (+155ms)
```
→ **3 並列起動**。最初の workflow が 14:25:29.947、次が 14:25:30.011、3 番目が 14:25:30.102 (合計 155ms 以内)。

### Run 2 (T14:25:51–14:26:13)
```
2026-05-04T142551_skill_improver.jsonl     # 71 events
2026-05-04T142551_skill_improver_1.jsonl   # 8 events
2026-05-04T142551_skill_improver_2.jsonl   # 21 events
```
→ **3 並列起動** (再現)。同タイムスタンプ prefix (142551)。

両セッションとも `invoke_skill(name="skill_improver")` がルーター LLM call 1 回から 3 件同時発行されている (dogfood_trace summary の tool call [1][2][3] および [31][32][33])。

---

## total tokens / LLM call 数

`dogfood_trace --mode cost` + budget ledger raw:

| 項目 | 値 |
|------|-----|
| 総 LLM calls (budget ledger) | **20 calls** |
| 総 tokens (budget ledger) | **112,082 tokens** |
| 実コスト (proxy 価格設定なし) | $0.000402 (router 2 calls のみ計上) |

内訳 (budget ledger より):
- router LLM call ×2: 1,616 + 1,713 = 3,329 tokens
- skill phase LLM calls ×18: 108,753 tokens (5,223〜6,613 tokens/call)

**batch 5 比較**: B5 では 333k tokens / 51 calls が観測された。今回は 2 セッション合計で 112k / 20 calls。差異は pexpect session が `/quit` で途中打ち切りになったため (B5 は完走した可能性あり)。並列 3 invoke のパターン自体は完全再現。

---

## Prediction hit/miss

### internal metric: 「50% で並列 invoke 再現」
→ **HIT**。両セッションとも 3 並列起動を確認。LLM 判断ばらつきなく決定論的に 3 件発行している。50% 予測は保守的すぎた (実態は high probability)。

### user metric: n/a (観測のみ)
→ 記録対象外。

---

## 並列誘発しなかった場合の挙動 (= LLM ばらつき範囲)

今回は両セッションとも 3 並列を再現したため、単一 invoke ケースは未観測。  
ただし WAL から以下のばらつきを確認:

- **Run 2 のうち 1 instance** (`skill_improver_2.jsonl`) は `prepare` phase で `ask_user` IR op を発行し、skill path 不在を報告。
- **別の instance** (`skill_improver.jsonl`) は `prepare → copy_to_work → run_and_eval` まで進行し、LLM が架空の `reyn/local/my_app/skill.md` path を補完した。

これは同一 invoke_skill call から生まれた 3 instance 間で LLM 判断が分岐していることを示す (= dedupe なしでは各 instance が独立に動く)。

---

## G3 dedupe fix 設計 evidence

| 証拠 | 詳細 |
|------|------|
| 並列 invoke 数 | 3 (両セッション一致) |
| 起動間隔 | 155ms 以内 (同一 tool call batch 由来) |
| Instance 間 LLM ばらつき | 同じ input でも path 補完・ask_user・copy_to_work と 3 種の判断が出た |
| token 浪費 | 3× の LLM call (= 単純に cost が 3 倍になる) |
| chain_peer_discarded | 2 件観測 (B peer discarded by user_discarded_skill_run / peer_discarded) |

**推奨 fix 方向**: ルーターが同一 skill を複数 invoke する tool call を発行した場合、OS 側で dedupe して最初の 1 件のみ起動する (= F5 sync dedupe の router 層拡張)。LLM output contract レベルでは "同一 skill への複数 invoke_skill は 1 件に圧縮" を validation ルールとして追加する案も検討。

---

## セッション打ち切り観測

- Run 1: pexpect が `/quit` を早期送信 → skill_improver 3 件はすべて truncated
- Run 2: 90s 待機後 `/quit` → 1 instance が `copy_to_work → run_and_eval` まで進行し eval skill を起動したが `run_and_eval` 途中で打ち切り
- `skill_runs/` state dir が `/quit` 後に消去される (crash recovery 観点で要注意)

---

## dogfood_trace 出力サマリー

```
[Skill Chain]  14 workflow(s) total (Run 1 + Run 2 合計)
  - skill_improver × 6 (3 per session)
  - eval_builder   × 4
  - eval           × 4 (2 per session)

[Tool Calls]  36 important tool calls
  - invoke_skill(skill_improver) × 6 (= 3+3)
  - file(read) × 20+ (skill.md / eval.md / improver_state.json)
  - run_skill × 4 (eval_builder / eval)
  - ask_user  × 1

[Peer Failures]  chain_peer_discarded × 2

=== Cost Summary ===
  Total: 112,082 tokens | 20 calls
  gemini-2.5-flash-lite (router): 3,329 tokens / 2 calls
  openai/gemini-2.5-flash-lite (skill phases): 108,753 tokens / 18 calls
```
