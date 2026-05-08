# B4-H1 [HIGH]: 「最後の 1 cm」 — narrator reply が agent_replies に届かない

> 一行で: B3-H1 fix で specialist が invoke_skill 到達するようになった。
> しかし narrator reply は **private `_put_outbox`** で送出されていて、
> RouterLoop の `agent_replies` collection に届かず、 user は curry recipe を
> 受け取らない。 attractor を抜けた先の OS routing layer の穴。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `ffc9b4a` |
| Scenario | S1 retest (multi-agent re-confirm) |
| Found | 2026-05-04 |
| Raw observation | [B4-retest-S1-S2.md](B4-retest-S1-S2.md) |

---

## 観測

batch 3 で B3-H1 fix (`48676ad`) を入れた後の retest。 specialist 側の挙動を
WAL で trace:

```
specialist RouterLoop:
  list_skills("")              → ok
  invoke_skill("direct_llm")   → 到達 ✅ (B3-H1 fix の効果)
  → direct_llm skill が curry recipe を生成 ✅
  → skill_narrator が reply text 生成 ✅

default 側の挙動:
  RouterLoop 終了時 _router_loop_agent_replies = [] (空)
  → B2-H2 fix path 発火: "specialist から処理結果が得られませんでした"
  → user に届く reply は 「peer 失敗」 のメッセージ
```

specialist は内部で完璧に動いた (= invoke 到達 / レシピ生成 / narrator 完了)、
それでも **user には curry が届かなかった**。

## つまり何が起きたか

deep dive で root cause 判明: `_run_skill_awaitable()` (= specialist の skill
完了後に narrator reply を outbox に流す経路) は **`self._put_outbox(...)`** を
呼んでいた。 これは default の outbox には message が入るが、 RouterLoop が
監視している `_router_loop_agent_replies: list[str]` collection には届かない
**(= update logic が `RouterLoopHost.put_outbox` callback 側にしかなかった)**。

結果:

- RouterLoop 終了時に `agent_replies` が空
- B2-H2 fix path (= 「peer reply failed surfaced」 の silent absorption 防止)
  が発火
- 「specialist から処理結果が得られませんでした」 が user に届く

つまり **B2-H2 fix が、 batch 3 で塞いだ attractor を抜けた経路に対して
誤検知** していた。 silent absorption fix と 正常 reply fix の boundary が
outbox routing layer で崩れていた。

これは「**fix した挙動 (silent absorption)**」 と「**fix してはいけない挙動
(正常完走の skill output)**」 の区別が API 設計に反映されていなかったこと
が原因。 prompt で fix すべき問題ではなく、 OS layer の routing fix が必要な
案件 (= memory `feedback_prompt_design.md` の「prompt vs code: 『prompt は
LLM 判断境界条件のみ』」 の事例)。

## 影響

- multi-agent UX の信頼性破壊 (= 内部成功でも user に届かない)
- B3-H1 fix の effectiveness が user 観点で機能しない (= 「fix した」 と
  「使えるようになった」 の最大の gap、 batch 3 retro で議論)
- B2-H2 fix の誤検知が出続ける限り、 multi-agent delegation 全般で同 issue

## 修正 (`ffc9b4a`)

`_run_skill_awaitable` 内側に **2 行 guard** 追加:

```python
await self._put_outbox(OutboxMessage(kind="agent", text=narrated, meta=meta))
# B4-H1: capture narrator reply for RouterLoop when active.
# _run_skill_awaitable calls the internal _put_outbox directly,
# bypassing the RouterLoopHost.put_outbox callback that normally
# updates _router_loop_agent_replies.  Mirror that logic here so
# specialist skill results reach _handle_agent_request / chain-resolve.
if self._router_loop_agent_replies is not None:
    self._router_loop_agent_replies.append(narrated)
```

`RouterLoopHost.put_outbox` (line 3143) の guard と完全 mirror。 fallback
(`skill_done`) 経路は **意図的に capture しない** (= 単体の "kind=agent"
narrator reply のみが user-facing reply text)。

`_append_history` の二重呼び出しは構造的に起きない (= `_run_skill_awaitable` は
`_append_history → _put_outbox` の順で呼ぶ、 `RouterLoopHost.put_outbox` は
本経路では呼ばれない) ことを Tier 2b 2 件で pin:

- `test_run_skill_awaitable_routes_to_router_loop_agent_replies`
- `test_no_double_history_append_on_agent_reply`

## 後続 (= batch 5 retest 2 で再 verify)

batch 5 fix-verify では prereq blocked (= B5-H1 prompt regression) で B4-H1
fix の e2e effectiveness が verify できなかった。 batch 5 retest 2 で B5-H1
+ H2 fix 後に再実行し、 narrator reply 経路を ✅ confirmed (= score=0.0
summary が user に到達)。

## 教訓

1. **「最後の 1 cm」 問題は OS routing layer に潜む**: prompt rule で attractor を
   抜けた後でも、 routing / outbox / state 管理 layer に別の穴があり得る
2. **fix の boundary が API 設計に反映されない時に誤検知が出る**: B2-H2 fix の
   「silent absorption 防止」 と本 case の「正常 reply」 を区別する API
   field (= explicit "is_silent_absorption" flag、 もしくは "is_skill_completed"
   flag) があれば誤検知は出なかった
3. **`_router_loop_agent_replies` の update 責務を 1 callback に集約**: 現状は
   `RouterLoopHost.put_outbox` callback と `_run_skill_awaitable` の 2 箇所で
   update 必要。 将来 OS refactor で 1 経路に集約すべき (= R-Dx 候補)
4. **internal metric (invoke 到達) と user metric (届く) を分離**: B4-H1 が
   暴露した最大の lesson、 batch 5 prediction で 2 metric 分離を試みる
