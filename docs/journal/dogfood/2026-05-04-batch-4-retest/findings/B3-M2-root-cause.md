# B3-M2 Root Cause: MCP catalog timing

| Field | Value |
|---|---|
| Status | investigation complete |
| Source | router_loop.py + session.py + mcp_client.py + router_system_prompt.py |

## 観測

B4 retest S2 の 2 試行:

- **Trial A** (`2026-05-04T115922`): LLM が `list_skills(path="read_local_files")` を呼んだが空配列が返り、`invoke_skill` に到達しなかった。
- **Trial B** (`2026-05-04T115950`): tool call が 0 件。router LLM が直接 text reply した。

`intervention_dispatched` / `ask_user IR op` はどちらも未観測。

## 検証した仮説

### 仮説 1: MCP server init が router LLM call より遅い

**verdict: 誤り (NOT the root cause)**

`read_local_files` は stdlib skill (`src/reyn/stdlib/skills/read_local_files/skill.md`) として静的にディスク上に存在する。MCP server (`filesystem`) は skill 実行フェーズ (`read_and_respond` の mcp op) で初めて呼ばれるものであり、catalog への登録とは無関係。

`enumerate_available_skills()` (session.py:323-361) は `reyn/project`, `reyn/local`, `stdlib/skills` の 3 ディレクトリをディスクから直接 walk し、`skill.md` の frontmatter を読む。ネットワーク通信なし、MCP server 起動待ちなし。`read_local_files/skill.md` は stdlib に存在するため、起動直後から catalog に含まれる。

**evidence path**: `session.py:331-360` の `enumerate_available_skills` — MCP 接続は一切行わない。

---

### 仮説 2: Skill catalog が router に渡る形式の問題 ← **ROOT CAUSE (主因)**

**verdict: 正 — list_skills の path パラメータ誤用による空応答**

`_list_skills(path)` (router_loop.py:409-433) の設計:
- `path=""` → skill を category でグループ化し `[{category, count}, ...]` を返す
- `path="<category>"` → その category に属する skill の `[{name, description}, ...]` を返す

`enumerate_available_skills` が返す各 entry に `category` フィールドは**存在しない**。session.py:355 で組み立てる entry は `{name, description}` のみ (routing があれば追加)。category フィールド未設定の場合、`_list_skills` は `"general"` にフォールバックする (router_loop.py:421)。

**LLM が `list_skills(path="read_local_files")` を呼ぶと**:
- `path = "read_local_files"` を category 名として解釈
- `(s.get("category") or "general") == "read_local_files"` にマッチする skill が 0 件
- `[]` が返る → LLM は skill が存在しないと誤判断し `invoke_skill` に到達しない

これが Trial A の観測と完全に一致する (`list_skills("read_local_files") ← [] (空)`、B4-retest-S1-S2.md:223)。

**コード line 参照**:
- `router_loop.py:409-433` (`_list_skills` の path=category 解釈)
- `session.py:323-361` (`enumerate_available_skills` が category を設定しない)
- `router_tools.py:108-128` (`list_skills` の tool description に「category path」と記載)

**副次問題**: `describe_skill(name="read_local_files")` を呼べば正しく取得できる (router_loop.py:435-440 は name で全 skill を線形探索)。しかし LLM は `list_skills` が空を返した時点で skill 不在と判断し describe_skill を試みない。

---

### 仮説 3: LLM 判断ばらつき

**verdict: 部分的に正 (contributing factor)**

Trial A と Trial B で挙動が異なる (Trial B は tool call 0 件の直接 reply)。これは gemini-2.5-flash-lite の出力分散であり、system prompt の routing rule (`list_skills → invoke_skill` 義務付け) が常に効くわけではない。ただし仮説 2 の `list_skills` 空応答が解消されても LLM 分散は残るため、根本ではなく増幅要因。

## Root cause

**主因 (B3-M2 の直接原因)**:

`list_skills` の `path` パラメータが **category namespace** として実装されているが、LLM は skill name が明示されたとき `list_skills(path="<skill_name>")` と呼ぶ。この呼び出しは category フィルタとして解釈されるため常に空配列を返す。

- `router_loop.py:409-433` — `_list_skills` が path を category 名として filter
- `router_tools.py:119-124` — tool description が `'Category path, e.g. "", "write", "write/blog"'` と記述しているが、LLM がこれを一貫して守らない
- `session.py:355` — `enumerate_available_skills` が category を設定しないため全 skill が `"general"` 扱い。`list_skills(path="general")` は機能するが、ユーザーが skill 名を明示した場合に LLM が間違った path を渡す

**副因 (Trial B の直接原因)**:

B3-H1 fix の system prompt rule (`After list_skills reveals at least one matching skill, Do NOT reply directly`) は list_skills が 0 件を返した場合をカバーしない。LLM が tool を呼ばず直接 reply する attractor は残存。

## Fix 候補

### 案 A: `list_skills` に skill name lookup モードを追加

`_list_skills` を拡張し、`path` が既存 category に一致しない場合は skill name として線形探索してフォールバック返却する。

```python
# router_loop.py _list_skills 内
# path が category に一致しない → name lookup にフォールバック
if path and not any_category_match:
    by_name = [s for s in skills if s.get("name") == path]
    if by_name:
        return [{"name": s["name"], "description": s.get("description", "")} for s in by_name]
```

**利点**: LLM の誤用パターン (`list_skills(path="read_local_files")`) を黙って吸収し正しい結果を返す。後方互換。  
**欠点**: category と skill 名の名前空間衝突時に曖昧。category 設計を将来拡張する際の混乱源になり得る。

### 案 B: system prompt + tool description を修正し describe_skill を誘導

`list_skills` が空を返したときの fallback rule を system prompt に追加:

> "If `list_skills(path=<category>)` returns empty and the user named a specific skill, call `describe_skill(name=<skill_name>)` directly."

また `invoke_skill` の tool description に「skill name は `list_skills("")` → category → `list_skills(path=<category>)` の順で確認するか、known name なら `describe_skill` で直接確認」を追記する。

**利点**: OS コードを変更せず prompt だけで修正。P7 原則に沿う。  
**欠点**: LLM 分散 (Trial B パターン) には効果が薄い。gemini-2.5-flash-lite の instruction-following が弱い環境では再現性が低い。

## 推奨

**案 A を推奨** (router_loop.py の `_list_skills` に name-lookup フォールバックを追加)。

理由:
1. LLM が skill 名を `list_skills` の path に渡すのは自然な誤用パターンであり、prompt だけでは根絶困難 (Trial B の 0-tool-call 事例が示す通り)。
2. フォールバックロジックは OS-generic (skill 名文字列を OS コードに埋めない) であり P7 違反にならない。
3. 実装が小さく副作用が少ない (category 照合が先行するため既存挙動は変わらない)。

案 B は案 A の補完として system prompt rule を 1 行追加するのみ実施し、Trial B パターンへの部分的対策とする。
