# B3-S4: narrator 品質 — B2-M4 再確認

> 一行で: narrator は 1 turn 目に README.md の具体的内容を返した。
> B2-M4 (「完了しました」のみ) は **再現せず**。 ただし router が同一 chain 内で
> 追加 LLM 呼び出しを行い、 narrator 後に seq=3 の重複的な agent reply が出る
> 副作用あり。

| Field | Value |
|---|---|
| Scenario | S4 |
| Batch | 3 |
| Date | 2026-05-04 |
| LLM | openai/gemini-2.5-flash-lite (LiteLLM proxy localhost:4000) |
| Prediction | 当たり 45% (1 turn 成功) |
| Verdict | **Prediction HIT** — 1 turn で内容あり |
| B2-M4 Status | 自然改善 (= open → resolved / fix 不要) |

---

## セットアップ

```bash
rm -rf .reyn/   # .reyn 不在を確認
# with-mcp.yaml を reyn.yaml として使用 (MCP filesystem 有効)
export OPENAI_API_KEY=dummy
reyn chat default --cui --no-restore
```

## Action

**Turn 1 (user)**:
```
read_local_files skill を使って README.md を読んで、 何の project か 1 段落で説明して
```

## 観測結果

### Turn 1 agent reply (seq=2, source=narrator)

```
README.md を読み込みました。Reyn プロジェクトは、予測可能性、監査可能性、
および自律性よりも制約を優先する LLM ワークフロー OS であると説明されています。
```

`meta.source = "narrator"` — 正規の narrator path を通過。
README.md の内容 (Reyn / LLM workflow OS / 予測可能性 / 監査可能性) が含まれる。
「完了しました」のみの無内容 reply は **出なかった**。

### 追加 agent reply (seq=3, source=router chain)

同一 chain_id (`11c549f14de446ba9dd062f2e0f5178e`) で router が LLM を追加呼び出し、
より詳細な Japanese 説明を生成:

```
このプロジェクトはReynという名前で、LLMワークフローOSとして説明されています。
予測可能性、監査可能性、および自律性よりも制約を優先することを特徴としています。
主な機能として、予測可能な制御フロー、制約された推論、およびマルチエージェント
構成モデルが強調されています。（以下略）
```

これは user の「1 段落で説明して」という要求に対してルーターが narratorのreplyに
続けて自前の説明を追加した動作。 UX 上は「内容が 2 回出る」 ように見える可能性あり。

### `:cost` 結果

| 項目 | 値 |
|---|---|
| 総 token | 29,977 |
| 推定コスト | $0.000909 (USD) |
| chain read_local_files token | 17,430 |
| skill 呼び出し | 1 回 |

コスト追跡は正常動作 (F4 residual fix 確認済み)。

### WAL / Events grep

```
skill_run_spawned:  read_local_files (T=11:09:48)
skill_run_completed: (T=11:09:53)  ← 5 秒
narrator workflow_started: (T=11:09:53)
narrator workflow_finished: (T=11:09:55)  ← 2 秒
tool_returned (invoke_skill): (T=11:09:55)
```

実行順序: `skill_started → decide_files → read_and_respond → workflow_finished`
→ `narrator workflow_started → narrate → workflow_finished`。
Narrator は skill の `final_output` を受け取り narration_request を構築、
`reply_text` を生成した (= context 伝達は正常)。

`mcp_called / mcp_completed` は read_local_files の read_and_respond phase で
1 回観測 (filesystem MCP 呼び出し正常)。

MCP teardown で anyio cancel scope RuntimeError が stderr に出現 (= B2-M3 再現)。

### 判定ポイント別確認

| 判定ポイント | 結果 |
|---|---|
| 1 turn 目に README.md の具体的内容 (Reyn/workflow 等) | **含まれる** ✅ |
| 「reyn run <skill>」等の internal CLI 命令滲み | **出なかった** ✅ |
| 「完了しました」のみで終わる | **出なかった** ✅ |
| B2-M4 再現 (2-turn 必要) | **再現せず** |

---

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **応答品質** | narrator が README.md 内容を含む日本語 1 文を生成。 router も追加で詳細説明を生成 |
| **意図解釈** | router が `describe_skill` → `invoke_skill` を正しく実行 (B2-H1 fix 有効) |
| **待ち時間** | skill 5 秒 + narrator 2 秒 = 計 7 秒。 実用許容範囲 |
| **見せ方** | `reyn run <skill_name>` 滲みなし。 seq=3 追加 reply あり (要確認) |
| **エラー UX** | エラーなし |
| **state 整合性** | events 順序正常。 narrator path 確認済み。 cost 記録正常 |

---

## 副作用観察: seq=3 router 追加 reply

narrator 完了後、 同一 chain で router が 1 回 LLM を追加呼び出しし、
より詳細な説明 (seq=3) を user に送信した。 これは:

- **UX impact**: user は同一質問に 2 つの agent reply を受け取る (重複感)
- **Severity**: LOW — 内容は正確で harmful ではないが、 体験として冗長
- **B2-M4 との関係**: B2-M4 当時は narrator が内容なしで終わり、 router が
  2 ターン目で補完するパターン。 今回は narrator が内容ありで、 router が
  同一 turn 内で追加補完するパターン。 改善はされているが冗長性が残る

この副作用は別途 B3-LOW として記録候補。

---

## B2-M3 再確認 (MCP teardown)

本 run でも MCP teardown 時の anyio cancel scope RuntimeError が stderr に出現:

```
Unhandled exception in event loop:
  File ".../mcp/client/stdio/__init__.py", line 183, in stdio_client
  Exception: Attempted to exit cancel scope in a different task than it was entered in
```

B2-M3 は open のまま継続。

---

## 結論

**B2-M4 は自然改善** (= fix 不要 / 自然解消カテゴリ)。 narrator の final_output
伝達経路は batch 3 時点で正常動作している。 ただし router が narrator reply に
続けて追加説明を生成する副作用 (seq=3) が新規観測された。 これは B2-M4 の
「無内容完了通知」 とは異なる、 軽度の冗長性問題として分類する。

prediction: **HIT** (1 turn で内容あり、 45% 当たり)。
