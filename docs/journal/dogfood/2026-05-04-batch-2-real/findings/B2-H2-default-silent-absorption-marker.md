# B2-H2 [HIGH]: default、 `_no_reply_marker` を飲み込んで「かしこまりました」

> 一行で: specialist から `_no_reply_marker` が届いたとき、 default がそれを
> user に伝えず「かしこまりました」と応じる — failure silent absorption。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** (commit to follow) |
| Scenario | S3 (Agent C — multi-agent delegate) |
| Found | 2026-05-04 |

> Context: B2-H1 と連動。 specialist が応答できなかった事実が、 default を
> 経由するうちに消えてしまう。 F7 (cascade 防止) とは逆の問題 —
> cascade を防ぎすぎて、 失敗を伝える responsibility まで消えた。

---

## 観測 (Agent C raw report)

specialist agent_response payload (WAL seq=5):

```json
{
  "response": "[specialist: could not produce a reply — router completed without producing a text reply]"
}
```

default が受信後に user へ返した文字列:

```
agent> カレーの簡単な作り方を教えてください。
agent> かしこまりました。 他に何かお手伝いできることはありますでしょうか？
```

1 行目は CUI 表示がリレーした request 文字列、 2 行目が default の実際の返答。
`_no_reply_marker` を認識した形跡がない。

## 期待との差

F6 修正の設計意図: specialist が応答を生成できなかった事実を
`_no_reply_marker` で明示的に表現し、 default がそれを受け取ったとき
ユーザーに「specialist は応答を生成できませんでした」と伝える。

現実: default の LLM は marker 文字列を「role: user/assistant の交換」 として
コンテキストに入れるが、 「これは失敗通知」 という意味を解釈できておらず、
通常の完了として扱う。 その結果、 `かしこまりました` という完全に誤誘導な
返答になる。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | default は「タスク完了」と誤解釈 |
| **応答品質** | 「かしこまりました」は技術的には正しくない |
| **待ち時間** | 4.6s (cascade 無いので速い) |
| **見せ方** | user は specialist が失敗したことを知る手段がない |
| **エラー UX** | non-actionable 最高峰: 失敗したのに「何か他に?」 |
| **state 整合性** | WAL には marker が記録される (audit trail は残る) |

## Severity guess

**HIGH** — これは F7 fix (cascade 防止) の副作用かもしれない。 「retry しない」
実装が「エラーを知らせない」 に変質している。 `_no_reply_marker` の認識を
default の system prompt か OS の agent_response handler に組み込む必要がある。

## Reproduction notes

```bash
reyn chat default --cui --no-restore
# user: "specialist エージェントに何かタスクを依頼する"
# B2-H1 が再現する環境では必ず B2-H2 も発生
# WAL grep: agent_response に _no_reply_marker が含まれることを確認
# CUI: "かしこまりました" または同様の completion reply が出たら再現
```

## Agent J 調査結果との連携

Agent J は B2-H1 と本 finding を並行調査 (research-only)。
`_no_reply_marker` の design intent vs 実装のギャップを含む。
fix wave 前に Agent J レポートを参照すること。

---

## 修正 (fix description)

Agent J の推奨 option (b): OS 側での決定論的 marker 検出。

### 変更ファイル

- `src/reyn/chat/session.py`:
  - `import re` 追加
  - `_is_no_reply_marker(text)` — structural detection helper
  - `_parse_no_reply_marker(text)` — (peer, reason) 抽出
  - `_NO_REPLY_MARKER_RE` — 正規表現パターン
  - `_PEER_REPLY_FAILED_MSG` — i18n dict (ja/en)
  - `_handle_agent_response`: user-initiated chain path に marker 検出を挿入 → LLM を bypass して直接 outbox へ
  - `_resolve_pending_chain`: pending chain path に marker 検出を挿入 → LLM を bypass して上流 agent へ転送
  - `_chat_events.emit("peer_reply_failed_surfaced", ...)` — audit event

- `tests/test_session_invariants.py`:
  - `test_peer_no_reply_marker_surfaced_to_user_not_absorbed` — user-initiated chain path
  - `test_peer_no_reply_marker_forwarded_upstream_in_pending_chain` — pending chain relay path

### 出力例

```
[ja] エージェント 'specialist' から処理結果が得られませんでした (理由: router completed without producing a text reply)。
[en] Could not get a result from agent 'specialist' (reason: router completed without producing a text reply).
```

marker format は変更なし (Agent J の推奨通り)。
