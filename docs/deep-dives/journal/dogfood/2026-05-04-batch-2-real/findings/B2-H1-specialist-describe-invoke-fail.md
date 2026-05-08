# B2-H1 [HIGH]: specialist の router、 describe まで行って invoke しない

> 一行で: specialist の RouterLoop が `list_skills` + `describe_skill("direct_llm")` まで
> 進みながら `invoke_skill` を呼ばず静かに止まる — F3 の亡霊が specialist 側に宿っていた。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `83bad83` (parallel race: B2-H2 agent が B2-H1 の差分も込みで先に commit、 commit message は B2-H2 とラベルされたが diff の中身は B2-H1) |
| Scenario | S3 (Agent C — multi-agent delegate) |
| Found | 2026-05-04 |

> Context: S3 は F5-F8 cascade の regression 確認。 cascade は解消したが、
> specialist の routing が新たな attractor にハマる様子が観測された。
> B2-H2 と連動する 2 件 HIGH。

---

## 観測 (Agent C raw report)

specialist が受信した request 「カレーの簡単な作り方を教えてください」 に対して
RouterLoop は以下の tool sequence を実行:

```
list_skills("")          → 10 skills in general (including direct_llm)
list_skills("general")   → 10 skills listed including direct_llm
describe_skill("direct_llm") → routing metadata returned
agent_message_sent       ← router loop 終了、 invoke_skill 呼び出し無し
```

WAL の `agent_message_sent` 時点で `agent_replies` は空 → F6 fix の
`_no_reply_marker` が発火 → default に「返答できなかった」 marker が届く。

## 期待との差

specialist は 「カレーの簡単な作り方」 への応答として `direct_llm` skill を選択
する、 もしくは直接 reply するはずだった。 実際は `describe_skill` で止まった。
F3 (router attractor for direct reply) の修正は **default agent の system prompt**
に施されたが、 specialist のルータープロンプトが同様の問題を持っているかどうかの
検証が抜けていた。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | describe → invoke の飛躍を踏み越えられず routing 失敗 |
| **応答品質** | user にカレーレシピは届かない |
| **待ち時間** | 4.6s で終了 (cascade 無し)、 ただし結果が無 |
| **見せ方** | default 側に silent absorption (= B2-H2) が加わり、 user は「かしこまりました」を受け取る |
| **エラー UX** | user 向けエラーは B2-H2 経由で消される |
| **state 整合性** | `tool_call_deduped` × 2 (F5 dedupe ✅)、 `agent_request` × 1 (F5 ✅)、 `agent_response` × 1 (空) |

## Severity guess

**HIGH** — multi-agent 実行の中核経路。 specialist が実質的に何も返せないため、
S3 の目標「カレーレシピが届く」 が未達。 F3 と同じ構造の attractor なので
同様の system prompt 修正で解消する見込みだが、 specialist プロンプトの
修正が必要 (default とは別ファイル)。

## Reproduction notes

```bash
reyn chat default --cui --no-restore
# user: "specialist エージェントに「カレーの簡単な作り方」を聞いて教えて"
# WAL: grep skill_phase_advanced → 0件 (specialist 側)
# WAL: grep tool_call_deduped → 2件 (正常 F5 dedupe)
```

## Agent J 調査結果との連携

Agent J は本 finding の詳細調査を並行実施 (research-only, no file changes)。
fix wave 前に Agent J レポートを参照すること。

## 修正

**修正方針**: `router_system_prompt.py` の Behaviour セクションに 1 ルール追加。
`describe_skill` 完了後に `invoke_skill` を呼ぶか、 呼ばない理由をテキストで
説明するかを義務付け。 「調査完了で無言終了」 の attractor を遮断する。

**変更ファイル**:
- `src/reyn/chat/router_system_prompt.py` — `describe_skill` 後コミット or
  説明ルールを `before invoke_skill.` 行の直後に追加
- `tests/test_router_system_prompt.py` — `TestBehaviourRulesAfterF3F9Fix`
  クラスに `test_post_describe_commit_or_explain` を追加 (Tier 2)

**非変更**: RouterLoop のコードパスは無変更 (prompt-only fix)。
コード level の retry 検出は Agent J が option (b) として退けた。
