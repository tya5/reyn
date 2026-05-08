# B2-M2 [MED]: tool_failed 後の fallback が output_language=ja 無視で英語

> 一行で: `invoke_skill` が失敗した後に router LLM が返す fallback 文が
> 英語 — F11 の ja 指令が tool_failed 経路で効いていない。

| Field | Value |
|---|---|
| Severity | MED |
| Status | open |
| Scenario | S1 (Agent A — text 要約) |
| Found | 2026-05-04 |

> Context: B2-M1 (skill 名 hallucination) の直後に発生する 2 次被害。
> F11 (router Japanese fallback fix) は正常経路で効いているが、
> tool_failed 後の回復経路がカバー外だった。

---

## 観測 (Agent A raw report)

`invoke_skill` tool_failed の直後に LLM が生成した fallback reply:

```
"It looks like the general.summarize skill is not available. I can still try to
summarize the text for you if you'd like, but I'll have to do it directly
without a specialized tool. Would you like me to proceed?"
```

完全な英語。 output_language=ja が session に設定されているにもかかわらず。

## 期待との差

F11 修正後は router fallback / clarifying path が日本語で応答するはずだった。
F11 の修正は router system prompt の「Behaviour」 セクションに適用されており、
正常経路 (= tool を呼ばず直接 reply) では機能する。 しかし tool_failed
という「例外経路」 では、 LLM が error recovery として英語でコンテキストを
再構築している可能性がある。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | skill 失敗を認識、 手動 fallback を申し出る (= 意図自体は良い) |
| **応答品質** | 内容は合理的だが言語が wrong |
| **待ち時間** | tool_failed 後即時 reply |
| **見せ方** | 英語でシステムの内情を説明する形になっている |
| **エラー UX** | 「Would you like me to proceed?」は user action を促すが ja で言ってほしい |
| **state 整合性** | tool_failed event は正常 |

## Severity guess

**MED** — B2-M1 が解消されれば通常は露出しない経路。 ただし
「skill が見つからない」 系のエラーは今後も発生しうるため、
tool_failed 後の recovery path にも output_language=ja 指令を
徹底する必要がある。 F11 修正の適用範囲を確認・拡張して対処。

## Reproduction notes

B2-M1 が再現している環境で自動追随する。
```bash
# WAL 確認: tool_failed → その次の inbox_put の message body が英語かチェック
# reyn.local.yaml: output_language: ja が設定されていること
```
