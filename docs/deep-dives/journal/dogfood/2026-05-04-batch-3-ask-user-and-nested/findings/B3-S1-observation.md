# B3-S1: multi-agent re-confirm — observation

| Field | Value |
|---|---|
| Scenario | S1 |
| Date | 2026-05-04 |
| main HEAD | e81f610 |
| Prediction | 当たり期待 70% |

## Action 入力

```
specialist エージェントに「カレーの簡単な作り方」 を聞いて教えて
```

セットアップ手順 (rm -rf .reyn/ 後):

```bash
reyn agent new specialist   # specialist agent 作成
reyn topology show _default # → members: default, specialist (自動参加確認)
reyn chat default --cui --no-restore --output-language ja
```

注: scenarios.md の Setup には `reyn topology` で specialist の存在確認とあったが、
`rm -rf .reyn/` 後は specialist agent が消えているため、事前に `reyn agent new specialist`
が必要。 この手順は scenarios.md に明記されていなかった — **B3-setup-gap** (後述)。

## CUI output (assistant reply)

```
[…] dispatched 2 async requests; awaiting peer reply
agent> エージェント 'specialist' から処理結果が得られませんでした (理由: router completed without producing a text reply)。
agent> 承知いたしました。カレーの簡単な作り方について、もう一度お伺いします。どのような情報をお探しですか？
```

カレーレシピは user に届かなかった。B2-H2 fix は機能し (peer_reply_failed_surfaced イベントと
エラーメッセージが表示された)、B2-H1 の specialist attractor は継続中。

待ち時間: 約 2.4 秒 (LLM 3 回 × ~0.8s)。ただし pexpect script の timeout 問題で
total session は ~147s かかった (script 側の待機ロジックの問題)。

## 観測 raw

### WAL grep 結果

#### skill_phase_advanced
```
(none)
```
specialist 側で skill が起動していないため、phase 遷移なし。

#### tool_called (invoke_skill / describe_skill / list_skills)
```
specialist: tool_called: list_skills args={'path': ''}      (11:12:24.165)
specialist: tool_called: list_skills args={'path': 'general'} (11:12:24.985)
specialist: tool_called: list_skills args={'path': 'food'}   (11:12:26.439)
```

`invoke_skill` / `describe_skill` は **一切呼ばれていない**。
specialist は `list_skills("")` → `list_skills("general")` → `agent_response (empty)` で終了。
2回目の request で `list_skills("food")` → `[]` (food カテゴリは空) → `agent_response (empty)`。

B2-H1 の describe→invoke attractor の修正後でも、specialist は now **list_skills のみで止まる**
別の attractor に陥っている。B2-H1 fix は describe→invoke を遮断したが、
list→invoke の attractor が残存している可能性。

#### peer_reply_failed_surfaced
```
default: peer_reply_failed_surfaced: peer=specialist reason=router completed without producing a text reply
  (11:12:25.808)
```

B2-H2 fix 経由でイベントが発火し、ユーザーにエラーが伝わった。H2 fix は機能。

#### agent_message_sent
```
default → specialist: agent_request  (11:12:23.291) ← 1回目
default → specialist: agent_request  (11:12:23.293) ← 2回目 (dedup なし？)
specialist → default: agent_response (11:12:25.753) ← 1回目応答 (empty)
specialist → default: agent_response (11:12:27.339) ← 2回目応答 (empty)
```

default から specialist へ agent_request が **2回**送られている。
F5 dedupe の `tool_call_deduped` イベントが default events に出ている (`tool_call_deduped`
が 1 件確認) にも関わらず、最終的に 2 request が specialist に届いた。
B2-H1 の再現と同じパターン。

#### skill_runs ls
```
(empty / not found)
```
skill が一度も invoke されていないため、skill_runs エントリなし。

### :cost

budget_state.json より:
```json
{
  "agent_tokens": {"default": 6540, "specialist": 7968},
  "agent_cost_usd": {"default": 0.0006921, "specialist": 0.00081300}
}
```

budget_ledger.jsonl (LLM 呼び出し 9 回):
```
default:    1594 tokens, $0.0001855  (11:12:23)
specialist: 1496 tokens, $0.0001535  (11:12:24) ← list_skills 後
specialist: 1542 tokens, $0.0001587  (11:12:24) ← list_skills("general") 後
specialist: 1872 tokens, $0.0001872  (11:12:25) ← agent_response 生成
specialist: 1514 tokens, $0.0001559  (11:12:26) ← list_skills("food") 後
specialist: 1544 tokens, $0.0001577  (11:12:27) ← 2回目 agent_response
default:    1620 tokens, $0.0001692  (11:12:28) ← peer fail surfaced + reply
default:    1651 tokens, $0.0001675  (11:13:54) ← :cost turn
default:    1675 tokens, $0.0001699  (11:14:09) ← :quit turn
Total: ~$0.00150 (cost > 0 確認 ✅、F4 教訓クリア)
```

:cost slash コマンドは CUI に表示されず (pexpect が cost 表示を capture できなかった)。
budget_ledger を直接読んで確認。

## 判定

| 項目 | 結果 |
|---|---|
| カレーレシピが user に届いた? | **no** |
| specialist が invoke_skill 呼んだ? | **no** (list_skills のみ、3回で停止) |
| `_no_reply_marker` 経路 (B2-H2 fix) 経由? | **yes** (peer_reply_failed_surfaced 発火、エラーメッセージ日本語で表示) |
| prediction 当たり? | **miss** (外れ予測の「chain 接続で新問題」パターン — ただし specialist 側でまだ止まる) |

## 6 軸

- **応答品質**: B2-H2 fix のエラーメッセージ (「エージェント 'specialist' から処理結果が得られませんでした」) は日本語かつ具体的。ただしカレーレシピは届かず品質評価不可。
- **意図解釈**: default の routing は正常 (delegate_to_agent で specialist へ)。specialist の router が list_skills → 停止 attractor に入り意図未達。
- **待ち時間**: specialist の routing は ~2.4s (3 LLM call × ~0.8s)。体感テンポは問題ないが結果が無い。
- **見せ方**: エラーメッセージが適切に日本語で表示された。「dispatched 2 async requests」は内部用語が滲むが許容範囲。
- **エラー UX**: B2-H2 fix により「かしこまりました」で silent absorption する問題は解消。ただしユーザーへの次アクション誘導 (「もう一度お伺いします。どのような情報をお探しですか？」) は若干的外れ — 「specialist が応答できませんでした、直接カレーレシピを提供します」の方が自然。
- **state 整合性**: events の順序は正常 (chat_started → agent_request_received → tool_called × N → agent_message_sent)。agent_request が 2 回送られている点は B2 時と同パターン。

## new finding 候補

**[HIGH] B3-H1: specialist の list_skills→invoke attractor が継続**
B2-H1 で fix した describe→invoke attractor が、list→invoke attractor として再現。
specialist の router が `list_skills("")` + `list_skills("general")` 後に
`invoke_skill` を呼ばずに `agent_response (empty)` で終了する。
B2-H1 fix (`router_system_prompt.py` の `describe_skill` 後コミットルール) では
list→invoke の遷移をカバーしていない可能性。

**[MED] B3-M1: setup 手順に agent 作成が未記載**
scenarios.md S1 の Setup セクションに `reyn agent new specialist` が抜けている。
`rm -rf .reyn/` 後は specialist が消えるため、次回 dogfood 実行者が再現できない。
scenarios.md の Setup を修正するか、dogfood common-setup に含める必要がある。

**[LOW] B3-L1: :cost CUI コマンドが capture できない**
pexpect で `:cost` 送信後の CUI 出力が捕捉されなかった。
`:cost` が slash コマンドとして処理されず、通常入力として LLM に送られている可能性。
budget_ledger.jsonl の直読みで workaround 可能だが、dogfood rig の改善余地あり。
