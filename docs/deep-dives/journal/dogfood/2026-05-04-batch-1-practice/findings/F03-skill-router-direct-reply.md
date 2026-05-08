# F3 [HIGH]: skill_router、 仕事しない大将

> 一行で: 「要約して」 とお願いしたのに `text_summarizer` skill は呼ばれず、
> LLM が「自分でやれます」 と直答。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `e59cead` (F3 + F9 同 commit) |
| Scenario | scenario 1 |
| Found | 2026-05-04 |

---

## 観測

scenario 1 で「次の英文を 3 つの bullet point に要約して」 と送信。 期待は
skill_router が `text_summarizer` skill を起動。 実態:

```
events/agents/default/skill_runs/2026-05-04*  → 存在しない
WAL: skill_dispatch event           → 存在しない
WAL tail:
  seq=205 inbox_put     (= user message)
  seq=206 inbox_consume (= agent picks it up)
  (続き — skill 関連 event 一切なし)
agent 応答:
  "* Python は 1991 年に Guido van Rossum によって作成された…"
  (= LLM が direct reply、 約 2 秒)
```

要約の中身は正しい。 日本語も自然。 だが **skill は呼ばれていない**。
LLM が直接答えただけ。

## つまり何が起きたか

Reyn の chat router は PR35 で native tool_use loop に置き換わった。 LLM が
利用可能 tool 一覧 (= skill / agent / memory / file / mcp) を見て、
「skill を起動する」 のか「直接答える」 のかを判断する。 今回 LLM は
「自分で要約できる」 と判断 (して問題ないと言えば問題ない) し、
text_summarizer を選ばなかった。

これは 2 通りの解釈:

- **(a) bug**: skill が catalog に登録されていて、 user が暗黙に「要約タスク」
  を頼んだのに、 router が skill を選ばないのは routing 失敗
- **(b) feature**: 軽量タスクは直接答えていい、 skill 起動は明示的に user
  が指定したときだけ

PR35 の dogfood 文脈 (memory: `project_dogfood_post_pr35.md`) では
「intent classification 改善ライン (R2-R7) は dogfood で urgency 出ず」
と記載されており、 当時は (b) として acceptable とされていた。 だが **F9
(scenario 3) で skill 名を明示してすら router が起動しなかった** ことを
合わせると、 これは (a) bug の方向に振れる。

## 影響

- text_summarizer skill が catalog に居る意味が薄い (= 呼ばれないので)
- skill 経由の固定品質パイプライン (= phase 構造、 schema 検証、
  preprocessor / postprocessor) を user が体感できない

## 修正 (commit `e59cead`)

`router_system_prompt.py` の Behaviour section に minimal disambiguation
hints を追加:

- "Reply directly only for chitchat, questions about yourself, and
  clarifications back to the user. Domain tasks → Action."
- "If the user names a skill, use list_skills + invoke_skill rather than
  paraphrasing the request as a Reply."

肥大化 / 重複 / 過剰適合を避けるため、 動詞 enumeration ではなく方針 2 行
のみ。 まず gemini-2.5-flash-lite でできるところまで完成度を上げ、 その後
強モデルで再評価する戦略。

## 後続 candidate (Wave B 以降)

- batch 2 で再現確認 (= regression net)
- gemini-2.5-flash-lite の routing 精度の限界に達したら router を strong
  model に固定する option
