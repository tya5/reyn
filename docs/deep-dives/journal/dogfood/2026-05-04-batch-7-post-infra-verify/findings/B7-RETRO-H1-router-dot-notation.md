# B7-RETRO-H1: router dot-notation hallucination — retroactive verification

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 269bdb6 |
| Original hypothesis | LLM が `skill_improver で direct_llm を` という入力を `skill_improver.direct_llm` という dot-notation スキル名に誤解釈する |
| Original verdict | blocked (B7-S1-observation.md) — chain 未起動、 router が dot-notation 名で invoke |
| **NEW verdict** | **verified (observation-based)** — `invoke_skill.name` に enum 制約なし、 LLM がスキル名を自由に生成できる構造が原因 |
| Trace file | `.reyn/llm_trace_h1.jsonl` |
| Main request_id | `561977d4-8272-4391-97a0-fe8145389dd4` (initial clean run) |

---

## Setup

```bash
rm -rf .reyn/
REYN_LLM_TRACE_DUMP=.reyn/llm_trace_h1.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
# input: skill_improver で direct_llm を 1 回 review して改善案を出して
# /quit
```

## Action

```bash
python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace_h1.jsonl
python scripts/dogfood_trace.py --mode llm-tools-schema 561977d4-8272-4391-97a0-fe8145389dd4 --trace .reyn/llm_trace_h1.jsonl
python scripts/dogfood_trace.py --mode llm-detail 561977d4-8272-4391-97a0-fe8145389dd4 --trace .reyn/llm_trace_h1.jsonl --full
```

## 実 payload 観測

### llm-payloads 出力 (initial clean run)

```
[T+0.0s] request_id=561977d4-...  model=gemini-2.5-flash-lite  caller=router  msgs=4  tools=11
[T+1.8s] response_id=561977d4-...  finish=tool_calls  tool_calls=3  tokens_in=1541  tokens_out=96
```

### invoke_skill tool schema (llm-tools-schema 出力)

```json
{
  "type": "function",
  "function": {
    "name": "invoke_skill",
    "description": "Run a skill. Construct input matching the skill's artifact schema ...",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {
          "type": "string",
          "description": "Skill name as listed by list_skills."
        },
        "input": {
          "type": "object",
          "description": "Skill input artifact: {type: <artifact_type>, data: {...}}"
        }
      },
      "required": ["name", "input"]
    }
  }
}
```

**enum 制約: 無し**。`name` は `type: string` のみ、enum なし。

### system prompt の skill list 表示

```
## Skills (resource axis, categories — use list_skills(path) to drill)
  general (10)
```

個別スキル名は system prompt に列挙されていない。カテゴリ数のみ。

### LLM response (初回 clean run)

```
tool_calls (3):
  - invoke_skill  args={"name": "skill_improver.review", "input": {"skill": "direct_llm"}}
  - invoke_skill  args={"name": "skill_improver.review", "input": {"skill": "direct_llm"}}
  - invoke_skill  args={"name": "skill_improver.review", "input": {"skill": "direct_llm"}}
```

### LLM response (再現性確認、 2nd run — 同一 wording)

```
tool_calls (3):
  - invoke_skill  args={"name": "skill_improver", "input": {"examples": [], "code": "direct_llm", "task": "review"}}
  - invoke_skill  args={"name": "skill_improver", "input": {"task": "review", "code": "direct_llm", "examples": []}}
  - invoke_skill  args={"name": "skill_improver", "input": {"examples": [], "code": "direct_llm", "task": "review"}}
```

2nd run ではスキル名は正しい (`skill_improver`) が input field は hallucination (`examples`, `code`, `task`)。
1st run は名前自体が hallucination (`skill_improver.review`)。どちらも失敗の構造は同じ。

## 旧推測との比較

| 項目 | 旧推測 | 観測結果 |
|---|---|---|
| 原因 | LLM が日本語 `で` を dot-notation として解釈 | **部分正解**。`で` + target 組み合わせが dot-notation を誘発する場合があるが、非決定論的 |
| enum 制約の有無 | 不明 (「enum 制約不在問題か?」と仮説) | **観測で確定**: `invoke_skill.name` は完全に自由な string — enum なし |
| system prompt の skill list | 不明 | **観測で確定**: カテゴリ名のみ (`general (10)`)、 個別スキル名は未列挙 |
| fix 方向の想定 | skill_router system prompt に「ドット区切り不可」を明示 | 同方向は有効だが、より根本的には enum 制約の追加か list_skills 強制の設計変更が必要 |

## 真因 (observation-based)

`invoke_skill.name` に **enum 制約が存在しない**。LLM は任意の文字列をスキル名として
生成でき、 `skill_improver.review` のような存在しない dotted name を hallucinate しても
schema レベルで reject されない。

加えて、 system prompt の skills section は `general (10)` というカテゴリ集計のみを
表示し、 個別スキル名を一切 inject しない。LLM は `list_skills` を呼ばずに直接
`invoke_skill` を試みる場合、 スキル名を「ゼロショット生成」する。

日本語 `skill_improver で direct_llm を` というパターンが dot-notation (`skill_improver.direct_llm`) 
または `skill_improver.review` のような「サブコマンド形式」へのバイアスを LLM に与えることが、
初回 run の `skill_improver.review` hallucination から観測される。ただし 2nd run では
正しいスキル名 `skill_improver` を生成したことから、 確率的挙動であることも確認。

## 修正方向 (observation-based)

1. **enum 制約追加**: `invoke_skill.name` に `enum: [<known skill names>]` を動的に
   inject する。list_skills の結果が既知の場合 (= router loop 内で既に呼んだ場合) は
   その結果から enum を生成。これにより schema 違反として即時 reject できる。
   ただし P7 制約: enum 値は OS が動的に決定し、OS コード内にスキル名リテラルは持たない。

2. **list_skills 強制**: router が `invoke_skill` を呼ぶ前に必ず `list_skills` を呼ぶよう
   tool_choice または system prompt で強制する。list_skills 結果が context に載れば
   LLM は実在名からしか選べなくなる。

3. **system prompt に flat skill list を inject**: `general (10)` カテゴリ集計のかわりに
   実スキル名一覧を system prompt に含める。ゼロショットの hallucination バイアスを排除。

優先度: **1 (enum) + 3 (flat list)** の組み合わせが最も直接的。2 は G12 attractor との
競合リスクがある (list_skills 後に invoke しない問題が別途存在するため)。

## next action

- `invoke_skill.name` への enum 動的 injection の設計 (= OS 層で skill registry から
  取得して tool schema を patch する仕組み) を次 wave で検討
- system prompt の skills section を `general (10)` から flat name list に変更する
  prototype を作成して効果測定
- この fix は B7-RETRO-H2 (eval_builder hallucinate) と共通原因 → 1 fix で 2 件解消の可能性
