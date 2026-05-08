# B7-G12: empty-stop 頻度測定 (ADR 0021 Option B フォローアップ)

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 71b9325 |
| Trace files used | `.reyn/llm_trace_g12_run2.jsonl` (fresh dogfood, attractor captured) |
| Attractor request_id | `883da2c8-adf6-4cff-b86a-a9a540f423ee` |
| Total replays | 10 (--n 10) |
| Empty-stop rate | **5/10 (50%)** |

---

## Setup

ADR 0021 の「残懸念点: Option B (OS retry) の rescue 率は観測データ不足」を解消するため、
既存 trace dump から attractor を含む request を抽出し `llm_replay.py --n 10` で頻度を測定した。

### 既存 trace 調査結果

既存 dump ファイル (`llm_trace_h4.jsonl`, `llm_trace_h2.jsonl`, `llm_trace_h1.jsonl`,
`llm_trace_b8s1.jsonl`) を `detect_attractor.py` で検査した結果、
すべてで `Detected attractors: 0` — G12 attractor なし。

B7-RETRO-H4 finding 文書に記載の attractor request_id `fd2aef81-1307-4bc8-9cea-f602d3b95d2a`
に対応する `.jsonl` ファイルはディスク上に存在しない (セッション終了時に削除済みと推定)。

### 新規 dogfood での attractor 誘発

worktree `agent-adbe9bd352bc7b644` 内で attractor 誘発シナリオを再実行:

```bash
# run1: attractor なし (4 router calls, すべて tool_calls で正常完了)
rm -rf .reyn/
REYN_LLM_TRACE_DUMP=.reyn/llm_trace_g12.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
# input: direct_llm skill を使って、カレーのレシピを教えてもらって

# run2: attractor 発生 (3rd router call で empty-stop)
rm -rf .reyn/
REYN_LLM_TRACE_DUMP=.reyn/llm_trace_g12_run2.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
# input: direct_llm skill を使って、カレーのレシピを教えてもらって
```

run2 で G12 attractor を捕捉。 `detect_attractor.py` 確認:

```bash
python scripts/detect_attractor.py \
  --trace .reyn/llm_trace_g12_run2.jsonl \
  --output-format json
```

```json
{
  "total_calls": 3,
  "summary": { "stop_with_must_rule": 1 },
  "detections": [{
    "request_id": "883da2c8-adf6-4cff-b86a-a9a540f423ee",
    "caller": "router",
    "heuristic": "stop_with_must_rule",
    "evidence": {
      "finish_reason": "stop",
      "completion_tokens": 0,
      "must_rule_excerpts": [
        "- After list_skills reveals at least one matching skill, you MUST",
        "- After describe_skill, you MUST call invoke_skill or explain in text"
      ]
    }
  }]
}
```

payload context (dogfood_trace.py llm-payloads):

```
[T+0.0s]  request_id=10a8c1d7...  caller=router  msgs=4  tools=11
[T+2.6s]  response_id=10a8c1d7...  finish=tool_calls  tool_calls=1  (list_skills(""))
[T+2.7s]  request_id=0fb9b4a7...  caller=router  msgs=6  tools=11
[T+5.4s]  response_id=0fb9b4a7...  finish=tool_calls  tool_calls=1  (list_skills("general"))
[T+5.4s]  request_id=883da2c8...  caller=router  msgs=8  tools=11
[T+7.8s]  response_id=883da2c8...  finish=stop  tool_calls=0  tokens_in=2271  tokens_out=0
```

B7-RETRO-H4 で確認された `list_skills("") → list_skills("general") → stop` の
sequence を再現。 `tokens_in=2271`、 `completion_tokens=0`。

## Action

```bash
LITELLM_API_BASE=http://localhost:4000 OPENAI_API_KEY=dummy \
  python scripts/llm_replay.py 883da2c8-adf6-4cff-b86a-a9a540f423ee \
  --trace .reyn/llm_trace_g12_run2.jsonl \
  --n 10 --diff
```

## Measurement results

### Run-by-run detail (request_id: `883da2c8-adf6-4cff-b86a-a9a540f423ee`)

| Run | finish_reason | tool_calls | completion_tokens | result |
|-----|--------------|------------|-------------------|--------|
| 1 | tool_calls | describe_skill("direct_llm") | 18 | rescued |
| 2 | stop | (none) | 0 | **empty-stop** |
| 3 | tool_calls | describe_skill("direct_llm") | 18 | rescued |
| 4 | stop | (none) | 0 | **empty-stop** |
| 5 | tool_calls | describe_skill("direct_llm") | 18 | rescued |
| 6 | tool_calls | describe_skill("direct_llm") | 18 | rescued |
| 7 | stop | (none) | 0 | **empty-stop** |
| 8 | tool_calls | describe_skill("direct_llm") | 18 | rescued |
| 9 | stop | (none) | 0 | **empty-stop** |
| 10 | stop | (none) | 0 | **empty-stop** |

### 集計

| Metric | Value |
|--------|-------|
| Total replays | 10 |
| Empty-stop (attractor 再発) | **5/10 (50%)** |
| Rescued (tool_call 成功) | 5/10 (50%) |
| Empty-stop 時 content | null / empty (全件) |
| Rescued 時 tool | describe_skill("direct_llm") (全件) |
| tokens_in (全件共通) | 2271 |
| tokens_out (empty-stop 時) | 0 (全件) |
| tokens_out (rescued 時) | 18 (全件) |

llm_replay.py N-shot summary:

```
Finish reasons:
  stop:       5 (50%)
  tool_calls: 5 (50%)
Tool calls (by name):
  describe_skill: 5 (50%)
N-shot diff summary (n=10):
  match=exact    : 5 (50%)  ← empty-stop 原本と一致
  match=different: 5 (50%)  ← rescued (tool_call 発生)
Finish reason matches: 5/10
```

### payload 共通点

全 10 run で同一 payload を使用:
- `caller=router`、 `msgs=8`、 `tools=11`
- messages に `list_skills("")` と `list_skills("general")` の tool result を含む
- system prompt に MUST rule (`After list_skills reveals at least one matching skill,
  you MUST call describe_skill or invoke_skill`) が injected
- `tool_choice: auto`

## Implication for ADR 0021 Option B

### 確率的 vs deterministic 判定

**判定: 確率的 (probabilistic)**

同一 payload で 10 回中 5 回 empty-stop、 5 回 rescued。 deterministic であれば
10/10 が empty-stop となるはずだが、 観測は 5/10。 attractor は **真に確率的**な挙動。

### retry rescue 期待値の見積

- empty-stop 率 p = 50%
- 1 retry で rescue される確率 = 1 − p = 50%
- つまり: Option B (1 retry) で 50% の attractor イベントが rescue される

計算例: 10 リクエストで 5 回 attractor 発生 → 1 retry で約 2.5 回追加 rescue →
残り約 2.5 回はそれでも attractor (2 retry なら 1.25 回、 …)

### retry 不要 / retry 効果薄 / retry 効果ある の判定

**判定: retry 効果あり (Medium-High)**

p = 50% は retry に十分な rescue 価値を持つ。 ADR 0021 の懸念
「deterministic なら retry 無意味」は **否定された**。
1 retry で期待値として attractor の半数が rescue される。

ただし残り 50% は retry でも empty-stop のため、 retry だけでは完全解消にはならない。
2 retry まで許容すれば期待 rescue 率は 75%、 3 retry で 87.5%。

## Recommendation update

### ADR 0021 Option B priority

**変更不要。 現行推奨 (Option B 短期採用) を維持。**

理由:
1. p = 50% の確率的 attractor は 1 retry で期待値 50% 改善できる — retry に意義あり
2. deterministic ではないため、 retry は無駄にならない
3. ADR 0021 が懸念した「retry しても同結果」ケースは全体の 50% に限られる
4. 残り 50% の「retry でも empty-stop」ケースは `router_attractor_retry` イベントで
   可視化され、 G4 spike (Option A / C) の evidence として継続蓄積できる

### 追加考察

- rescued 時のツール呼び出しは全件 `describe_skill("direct_llm")` — モデルが
  「正解」を知っているが確率的に出力できない状態
- `completion_tokens=0` は truncation (内部生成開始→中断) の可能性があり、
  単純な「意図的 stop」とは異なる provider 挙動
- 2 retry 許容 (期待 rescue 率 75%) が cost-benefit 的に optimum と思われるが、
  この判断は Option B 実装時に決定すれば十分

## LLM cost

| Item | Count | Tokens/call | Estimated cost |
|------|-------|-------------|----------------|
| Fresh dogfood run1 | 4 router + 3 phase calls | ~1900 avg | ~$0.0026 |
| Fresh dogfood run2 | 3 router calls | ~2100 avg | ~$0.0006 |
| llm_replay --n 10 | 10 calls | 2271 in / 0-18 out | ~$0.0025 |
| **Total** | | | **~$0.006** |

## References

- `scripts/detect_attractor.py` — Heuristic 1 (`stop_with_must_rule`) 使用
- `scripts/llm_replay.py` — `--n 10 --diff` で N-shot replay
- `docs/en/decisions/0021-g12-attractor-structural-fix-design.md` — ADR
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-RETRO-H4-attractor-prompt-evidence.md`
  — 前回 root cause 確定 finding
