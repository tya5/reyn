# G4 Trigger Evaluation Spike

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| baseline (weak) | gemini-2.5-flash-lite |
| candidate (strong) | N/A — proxy not ready |
| Scenario | batch 6 S2 (= specialist + curry recipe) |

## Status: BLOCKED — Proxy 整備が prerequisite

LiteLLM proxy (`http://localhost:4000`) が強モデルを提供していない。

### proxy 調査結果

```
GET /v1/models → ["codex-proxy", "gemini-2.5-flash-lite"]
```

- `codex-proxy`: `/Users/yasudatetsuya/Workspace/junk/litellm/config.yaml` で
  `openai/codex-model → http://localhost:4000/v1` にループしている (= 自己参照、
  強モデルではない)
- `gemini-2.5-flash-lite`: 通常の weak LLM (= baseline と同一)
- `claude-sonnet` / `gemini-2.5-pro` は未登録

spike 実行の前提条件を満たしていないため、 実試行は **断念**。

## 5 試行結果 (strong model)

実施不可。

| Run | attractor? | LLM calls | tokens | USD |
|-----|-----------|-----------|--------|-----|
| 1 | — (未実施) | — | — | — |
| 2 | — | — | — | — |
| 3 | — | — | — | — |
| 4 | — | — | — | — |
| 5 | — | — | — | — |

## baseline (weak) 比較

| Metric | weak | strong | ratio |
|---|---|---|---|
| attractor rate | 4/4 (100%) — batch 2/3/5/5R2 連続 | N/A | — |
| avg LLM calls | 観測値なし (batch 別 doc 参照) | — | — |
| avg tokens | 観測値なし | — | — |
| avg USD | ~$0.001 per run (flash-lite 推定) | — | — |

weak LLM の baseline は `giveup-tracker.md` G12 section の history 参照:
- B2-H1 / B3-H1 / B5-H1 / B5R2-H1 = 4 batch 連続 attractor 再発
- MUST rule が prompt に存在するにも関わらず honor されない確率的不honor 確定

## ROI 判定

**判定不能 (proxy 未整備)**

proxy に強モデルが追加されてから再実施が必要。

## 推奨

### 即時: proxy 設定追加

`/Users/yasudatetsuya/Workspace/junk/litellm/config.yaml` に以下いずれかを追加:

**Option A: Anthropic Claude Sonnet (推奨)**

```yaml
  - model_name: claude-sonnet
    litellm_params:
      model: anthropic/claude-sonnet-4-5  # または claude-3-5-sonnet-20241022
      api_key: os.environ/ANTHROPIC_API_KEY
```

環境変数: `export ANTHROPIC_API_KEY=<your-key>`

**Option B: Google Gemini 2.5 Pro**

```yaml
  - model_name: gemini-2.5-pro
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_key: os.environ/GOOGLE_API_KEY
```

(= 既存 GOOGLE_API_KEY が有れば追加コスト設定不要)

その後 `reyn.local.yaml` を一時変更:

```yaml
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/claude-sonnet   # または gemini-2.5-pro
  strong:   openai/claude-sonnet
```

### spike 再実施手順

proxy 整備後:

```bash
cd <worktree>
# 5 回繰り返し:
rm -rf .reyn/
reyn agent new specialist 2>/dev/null || true
reyn topology show _default
# input: specialist エージェントに「カレーの簡単な作り方」を聞いて教えて
reyn chat default --cui --no-restore
# 終了後:
python scripts/dogfood_trace.py --mode summary
python scripts/dogfood_trace.py --mode cost
```

各回の attractor 発生 / LLM calls / tokens / USD を本 doc の表に記録し、
`giveup-tracker.md` G12 を更新。

### G12 への暫定影響なし

proxy 整備前は G12 status を `active (Wave 3 spike blocked — proxy 未整備)`
に更新。 spike 再実施後に最終 ROI 判定を行う。

## raw 観測 data

実施不可のため省略。

proxy 調査コマンド実行結果:

```
$ curl -s http://localhost:4000/v1/models | python3 -m json.tool
{
    "data": [
        {"id": "codex-proxy", ...},
        {"id": "gemini-2.5-flash-lite", ...}
    ]
}

$ cat /Users/yasudatetsuya/Workspace/junk/litellm/config.yaml
model_list:
  - model_name: codex-proxy
    litellm_params:
      model: openai/codex-model
      api_base: http://localhost:4000/v1   # ← 自己参照ループ
  - model_name: gemini-2.5-flash-lite
    litellm_params:
      model: gemini/gemini-2.5-flash-lite
      api_key: os.environ/GOOGLE_API_KEY
```

claude-sonnet / gemini-2.5-pro のいずれも未設定。
