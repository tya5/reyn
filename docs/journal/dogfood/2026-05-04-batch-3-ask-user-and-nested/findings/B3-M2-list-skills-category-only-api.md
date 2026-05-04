# B3-M2 [MED]: 「`read_local_files` 使って」 と頼まれてるのに skill を呼ばない

> 一行で: skill 名を明示しても router が tool 呼ばず direct reply を返す
> 挙動は、 prompt 問題ではなく **`_list_skills(path)` の category-only API**
> が name で叩かれて空配列を返す code design 問題だった。

| Field | Value |
|---|---|
| Severity | MED |
| Status | **fixed** at `d6a987b` (root cause investigation `908b21e`) |
| Scenario | S2 (ask_user e2e、 ただし主因は別の root cause として顕在化) |
| Found | 2026-05-04 |
| Raw observation | [B3-S2-observation.md](B3-S2-observation.md)、 [B3-M2-root-cause.md](../../2026-05-04-batch-4-retest/findings/B3-M2-root-cause.md) |

---

## 観測

batch 3 S2 で user input `read_local_files skill を使って report.md を読んで
要約して` を 4 turn 全てで送ったが、 router は **`list_skills` も
`invoke_skill` も呼ばず**、 direct text reply のみで応答。 events log:

```
tool_called: 0 件 (= 全 turn)
skill_started: 0 件
intervention_dispatched: 0 件 (= ask_user 観測機会も消失)
```

batch 4 retest S2 で同 scenario を再実行しても **trial A** (= list_skills は
呼んだが catalog に read_local_files が出ない) と **trial B** (= tool 呼ばず
direct reply) で挙動分散。 「LLM 判断ばらつきの一過性問題」 と当初解釈した。

## つまり何が起きたか (root cause investigation `908b21e`)

batch 4 retest 後に root cause investigation を実施し、 仮説 3 件を検証:

1. ❌ MCP server init timing (= read_local_files が catalog に遅れて反映)
2. ❌ Skill catalog 形式の問題
3. ✅ **`_list_skills(path)` の API design** が真の root cause

`_list_skills(path)` は `path` 引数を **category** として扱う設計
(`router_loop.py:409-433`)。 ところが:

- `enumerate_available_skills()` (`session.py:323-361`) は skill の `category`
  field を設定していない → 全 skill が `"general"` 扱い
- LLM は `path` を **skill name** と解釈して呼ぶ (= 「`read_local_files` を
  探したい」 → `list_skills(path="read_local_files")`)
- category filter は `"general" == "read_local_files"` で **空配列** 返却
- LLM は「skill 不在」 と誤判断、 direct reply に逃げる

つまり:

- LLM 判断は **正しい** (= `read_local_files` を探そうとしてる)
- API 設計が **LLM の合理的呼び出しに対応していない**

`describe_skill(name="read_local_files")` を呼べば name 線形探索
(`router_loop.py:435-440`) で正しく取れるが、 `list_skills` で空が返ると
LLM はそこに至らない (= attractor)。

## 影響

- skill 名明示 user request の半分以上で skill 起動失敗、 user 視点では
  「skill を指定したのに無視される」
- ask_user IR op 観測が依然 dark (= skill 起動しないと IR op まで到達せず)
- B3-H1 / B3-H2 の attractor family と同 root cause かと当初誤認、 fix の
  方向性を見誤りかけた

## 修正 (`d6a987b`、 Option A)

`_list_skills(path)` に **name lookup fallback** を追加:

```
1. path 空 → 全 skills (既存)
2. path == category → category の skills (既存)
3. category 不一致 → name で線形探索 (新規)、 hit すれば 1 件 list で返す
4. name も不一致 → 空 array (= 既存挙動と一致)
```

実装は `router_loop.py` に **9 行追加**。 prompt 触らず code-only 解決
(= memory `feedback_prompt_design.md` で formalize した「prompt rule 追加で
fix 可能でも、 root cause が code design なら code 側で直す」 を実践)。

3 Tier 2 test 追加:

- `test_list_skills_name_lookup_fallback`
- `test_list_skills_unknown_path_returns_empty`
- `test_list_skills_empty_path_returns_all_categories` (regression net)

## 後続 candidate

- batch 5+ で `read_local_files` 経由の ask_user e2e を再実行、 IR op 観測
  まで到達できるか確認
- 同 family の API design 問題が他にもないか audit (= LLM の合理的呼び出しに
  対応していない API を grep で発見)

## 教訓

1. **prompt rule で fix 可能 ≠ prompt rule で fix すべき**: B3-M2 は prompt
   で「list_skills が空のときは describe_skill を試せ」 を加えれば部分対症療法
   できたが、 root cause は code design。 prompt rule 追加路線は memory
   `feedback_prompt_design.md` の bloat trap に陥る
2. **investigation step を docs に残す価値**: root cause investigation
   `908b21e` を別 doc (`B3-M2-root-cause.md`) として保存。 「3 仮説を検証して
   1 つに絞る」 という process を後から辿れる形で記録
3. **LLM の「合理的呼び出し」 を API が拒む設計は debug 時間を奪う**:
   `_list_skills(path)` の path 解釈を「category または name」 に拡張する
   だけで attractor が消えた。 API 設計時に「LLM はこの引数をどう解釈するか」
   を考慮すべき
