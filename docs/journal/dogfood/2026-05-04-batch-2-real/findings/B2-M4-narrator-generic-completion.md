# B2-M4 [MED]: narrator の「完了しました」が空虚 — skill 出力が届かない

> 一行で: narrator が skill 完了後に「スキルが正常に完了しました」と言うだけで
> file の内容を提示せず、 router が 2 ターン目でフォローアップする 2-turn 体験。

| Field | Value |
|---|---|
| Severity | MED |
| Status | open |
| Scenario | S2 (Agent B — `read_local_files` + MCP) |
| Found | 2026-05-04 |

---

## 観測 (Agent B raw report)

`read_local_files` 成功後の narrator が生成した reply:

```
"スキルが正常に完了しました。 もし別のスキルを試したい場合は、
 reyn run <skill_name> を実行してください。"
```

README.md の内容については一切触れていない。 その後 router が 2 ターン目で
内部に残った skill 出力を読み、 ようやく Japanese summary を提示した。

## 期待との差

1 ターンで「README.md の内容: [summary]」 が返るはずだった。
narrator は skill の final_output を受け取り、 それを自然言語でラップして
user に提示する責務を持つ。 今回は final_output が narrator のコンテキスト
に正しく渡っていないか、 narrator prompt が「完了通知」モードに固定されている。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | skill は正しく動いた |
| **応答品質** | 1 ターン目の reply は無内容 |
| **待ち時間** | 2-turn 分の LLM 呼び出しが発生 (= 体感 2 倍) |
| **見せ方** | 「reyn run <skill_name>」という CLI 指示が user に滲む — internal |
| **エラー UX** | 失敗ではないが体験として中途半端 |
| **state 整合性** | skill_completed event は 1 件 (正常) |

## Severity guess

**MED** — skill pipeline の最終段、 user に価値を届ける最後の 1cm 問題。
技術的には動いているのに体験として 2-turn 必要という状況はカテゴリ的に
F3 と似ている (= 機能はあるが user に届いていない)。 narrator の
final_output 伝達経路とプロンプトを確認・修正が必要。

## Reproduction notes

```bash
reyn chat default --cui --no-restore --config with-mcp.yaml
# user: "read_local_files skill を使って README.md を読んで説明して"
# agent 1st reply: "スキルが正常に完了しました" → skill 内容が無ければ再現
# agent 2nd reply: (フォローアップで内容提示)
```
