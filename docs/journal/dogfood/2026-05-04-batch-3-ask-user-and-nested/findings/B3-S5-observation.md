# B3-S5 [MED]: skill 名 hallucination — partial improvement、 invoke_skill 未発火

> 一行で: `list_skills` が `invoke_skill` より先に呼ばれるようになった (B2-M1 修正の効果あり)。
> しかし LLM が skill 一覧に `text_summarizer` を確認した後、 `invoke_skill` せず direct reply。
> hallucination 自体は消えたが、 skill 実行経路が開通していない。

| Field | Value |
|---|---|
| Severity | MED (継続 open) |
| Status | partial improvement — B2-M1 の hallucination は解消、新パターン出現 |
| Scenario | S5 (skill 名 hallucination 再確認) |
| Found | 2026-05-04 |
| HEAD | `e81f610` |

---

## 観測

### tool_call sequence (events/agents/default/chat/2026-05/2026-05-04T111306.jsonl)

```
11:13:08 tool_called   list_skills  args={"path": ""}
11:13:08 tool_returned list_skills  result=[{"category": "general", "count": 23}]
11:13:09 tool_called   list_skills  args={"path": "general"}
11:13:09 tool_returned list_skills  result=[..., {"name": "text_summarizer",
                                                   "description": "Summarizes long text into 3 bullet points."}, ...]
(invoke_skill は一度も呼ばれず)
```

`tool_failed` event: **なし** (= hallucination による invoke 失敗はゼロ)

### invoke_skill の name フィールド

`invoke_skill` は **呼ばれなかった**。 B2-M1 の再現 (general.summarize 発明) は消えている。

### 3 ターン目 LLM の選択 (budget_ledger より: 11:13:10 に 3 回目の LLM call あり)

router_cap = 3。 1 回目 → list_skills("")、 2 回目 → list_skills("general")、
3 回目 → **direct reply** (invoke_skill なし)。

LLM は `text_summarizer` を skill 一覧で確認したにもかかわらず、 invoke せず直接回答。

### 応答内容 (history.jsonl seq=3、 :cost chain に対して)

```
Pythonについての要約を3つの箇条書きで示します。

*   Pythonは1991年にGuido van Rossumによって作成されたハイレベルプログラミング言語です。
*   コードの可読性を重視しています。
*   複数のプログラミングパラダイムをサポートしています。
```

**言語: 日本語** (B2-M2 連動確認 → fallback 英語問題は今回未発火)

### CUI 上の fallback テキスト (run 1 — .reyn/ 未クリア状態で初回に観測)

```
agent> 申し訳ありませんが、「general.summarize」というスキルが見つかりませんでした。
       代わりに「text_summarizer」というスキルを使用できます。
```

run 1 は .reyn/ にキャッシュが残っており B2 状態からの継続。 run 2 (rm -rf .reyn/ 後) では
この hallucination は再現せず。 clean state では partial improvement を確認。

### :cost

```
budget_ledger:
  11:13:08  gemini-2.5-flash-lite  tokens=1874  cost=$0.0001913
  11:13:09  gemini-2.5-flash-lite  tokens=1920  cost=$0.0001965
  11:13:10  gemini-2.5-flash-lite  tokens=2599  cost=$0.0002599
合計 3 LLM calls、 cost > 0 ✅ (F4 教訓クリア)
```

---

## 判定ポイント照合

| 判定項目 | 結果 |
|---|---|
| `list_skills` が `invoke_skill` より先に呼ばれたか | **YES** — list_skills × 2 → no invoke_skill |
| `tool_failed` (hallucination) が出たか | **NO** — hallucination は解消 |
| `invoke_skill` の name が実在 skill か hallucination か | **N/A** — invoke_skill 未発火 |
| fallback reply の言語 | **日本語** (B2-M2 は今回非発火、連動確認不要) |

---

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | list_skills → text_summarizer 確認まで正しい。 しかし invoke せず direct reply |
| **応答品質** | direct reply は正確な日本語 3 bullet。 内容は合格 |
| **待ち時間** | 3 LLM calls (約 2 秒)。 invoke_skill しないため速い |
| **見せ方** | agent reply のみ表示。 内部の list_skills は CUI に出ない |
| **エラー UX** | エラーなし。 ただし skill を使わなかったことは user には不透明 |
| **state 整合性** | tool_called × 2、 compaction_check 正常、 history に direct reply 格納 |

---

## 事前 prediction との照合

prediction: **当たり期待 30%**、 外れ典型 = 「B2-M1 再現 or partial improvement」

**結果: partial improvement で 外れ 寄り正解**。
- hallucination は消えた (= `list_skills 先行` 指示が機能)
- しかし「invoke_skill を使わず direct reply」という新パターン出現
- scenarios.md の外れ予測「hallucination は無いが期待 skill も使わない中間結果」が正確に的中

---

## 新規 finding: B3-S5-NEW — router が skill 確認後も invoke_skill を選ばない

### 原因仮説

router system prompt の Behaviour セクション (router_system_prompt.py L167-169) に:
```
- For Action, browse list_skills (then describe_skill if needed)
  before invoke_skill.
```
と書かれているが、 「list を見て適切な skill があれば必ず invoke_skill を選べ」 という
obligation が明示されていない。 LLM は list を見て「自分で直接答えられる」と判断すると
invoke_skill をスキップする attractor に落ちる。

### 修正候補

router system prompt に obligation を追加:
```
- After list_skills reveals a skill that matches the user's task,
  you MUST call invoke_skill with that skill name; do NOT reply directly.
  Only use Reply if no skill matches (= direct_llm is the fallback).
```

### Severity

**MED** — ユーザーには正しい回答が届く。 ただし skill ecosystem を bypass するため
コスト・品質保証・audit trail (P6) の恩恵が得られない。

---

## Reproduction notes

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
reyn chat default --cui --no-restore
# user: "次の英文を 3 つの bullet point に要約して: Python is a high-level ..."
# WAL grep: tool_called list_skills が先行、 invoke_skill が出ないことを確認
grep tool_called .reyn/events/agents/default/chat/**/*.jsonl
# → list_skills × 2 のみ、 invoke_skill なし
```
