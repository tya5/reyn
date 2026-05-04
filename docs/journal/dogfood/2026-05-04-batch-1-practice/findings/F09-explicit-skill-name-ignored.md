# F9 [HIGH]: skill 名を明示してもなお、 router は応えない

> 一行で: `read_local_files skill で〜` と skill 名を本文に直書きしても
> router は無視。 routing 0/3。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `e59cead` (F3 と同 commit) |
| Scenario | scenario 3 (read_local_files permission gating) |
| Found | 2026-05-04 |

---

## 観測

scenario 3 で「**read_local_files skill で** /path/to/README.md を読んで
要約して」 と、 **skill 名を本文に直書きして** リクエスト。 期待は router が
迷わず read_local_files を起動すること。

実態:

```
WAL tail:
  seq=221 inbox_put     (user message)
  seq=222 inbox_consume (agent)
  (続き — skill_dispatch なし)

events skill_runs/2026-05/  → 2026-05-04 entry 無し

agent 応答:
  "I noticed you asked to read and summarize the file ... twice.
   Could you please clarify if you need me to perform this action,
   or if you were testing something?"
```

skill router 起動の形跡無し。 LLM は「同じこと 2 回聞かれた」 と
hallucinate して clarifying question を返した (英語で。 F11)。

## つまり何が起きたか

F3 の延長線。 implicit な「要約して」 で起動しないだけでなく、 **explicit に
skill 名を挙げても起動しない**。 これは router の routing が壊れている
ことを示唆。

routing 失敗率: **0/3 across all scenarios**。 = router は仕事をしていない。

## 影響

- skill 経由のあらゆる UX (= startup_guard / phase / preprocessor /
  postprocessor / chain delegate) が user に届かない
- Reyn の中核価値 (= 構造化された LLM workflow) が user 視点で消失
- これが **「現状人間視点だと chat の会話は使い物にならない」 の正体の一部**

## 修正 (commit `e59cead`)

F3 と同じ修正。 system prompt に "If the user names a skill, use list_skills
+ invoke_skill rather than paraphrasing the request as a Reply." を追加。

## 後続 (Wave B 以降)

- skill_router の routing 判定の徹底再点検
- system prompt / tool offering / 模範例 prompt の audit
- 場合によっては router を「強制 skill 起動 mode」 に切り替え可能にする
  flag (= operator が「skill 必ず通せ」 と指定できる)
- batch 2 で再現確認 (regression net)
