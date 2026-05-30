---
type: concept
topic: universal-catalog
audience: [human, agent]
---

# Universal Action Catalog (汎用アクションカタログ)

Reyn agent の chat router はもともと、 discovery surface ごとに別々の
tool を露出していた — `list_skills` / `list_mcp_tools` / `list_memory` /
`list_agents` / … と、 種類ごとの `invoke_*`。 カタログが成長するにつれ
LLM に見せる tool 一覧も線形に膨らみ、 新しい resource 種別を足すたびに
LLM が学ぶべき tool が 1 つ増えていた。

**Universal action catalog** (FP-0034) は、 種別ごとの N 個の discover /
describe / invoke tool を、 **全 category を一律にカバーする 4 つの
wrapper** に置き換える。 あらゆる action — skill / peer agent / MCP
tool / memory entry / file op / indexed corpus / … — は単一の qualified
name (`<category>__<entry>`) でアドレッシングされ、 `invoke_action` 経由
で dispatch される。 discovery は `list_actions`、 詳細 introspection は
`describe_action`、 自然言語 / semantic 検索は `search_actions`
(embedding-backed) で扱う。

Phase 6 (2026-05-16) 以降、 **wrapper-only path が production 既定**:
legacy per-kind tool は LLM 可視の `tools=` に出ない。 handler
(`invoke_skill` / `delegate_to_agent` / `call_mcp_tool` / …) は wrapper
の **backing implementation** として registry に残存 — `invoke_action`
が `universal_dispatch.py` 経由で dispatch する。 Validation: dogfood
batch 26 N=5 stability (= 32/35 = 91.4% verified、 Brier 0.177、
hallucination 0/35)。

## なぜ単一カタログか

| 種別ごとカタログ (legacy) | 汎用カタログ (FP-0034) |
|---|---|
| resource 種別ごとに N 個の discover tool | 1 つの `list_actions(category=[…])` |
| resource 種別ごとに N 個の describe tool | 1 つの `describe_action(action_name)` |
| resource 種別ごとに N 個の invoke tool | 1 つの `invoke_action(action_name, args)` |
| LLM tool 数は surface に対して線形 | LLM tool 数は constant |
| 新種別追加には新 tool が要る | 新種別追加は category + dispatch rule 1 件 |
| tool ごとに同じ discover→describe→invoke pattern を再記述 | 1 つの pattern を 1 箇所で記述 |

アーキテクチャ上の win は **LLM の tool 数が resource category 数に対し
て O(1) になる** こと。 14 番目の category を足しても 14 番目の tool は
増えない — `CATEGORIES` tuple に 1 行 + routing rule 1 件で済む。

## Category 一覧 (§D18 master taxonomy)

| Category | 保持するもの | Canonical invoke 意味論 |
|---|---|---|
| `skill` | project / stdlib skill | `input` artifact を持って skill を実行 |
| `agent.peer` | topology 内の peer agent | その peer にメッセージを delegate |
| `mcp` | MCP server 管理 + tool dispatch | 6 個の verb_object actions — 下表参照 |
| `file` | workspace の file op | read / write / delete / list |
| `web` | web search + fetch | search または fetch |
| `memory.entry` | 永続 memory record | entry の body を read |
| `memory.operation` | memory CRUD op | `remember_shared` / `remember_agent` / `forget` |
| `reyn.source` | Reyn source / docs (read-only) | read または list |
| `rag.corpus` | indexed corpora (resource) | この single source に対して recall |
| `rag.operation` | RAG 管理 op | multi-source recall / drop source |
| `exec` | sandboxed argv 実行 | sandbox backend 下で argv 実行 |

`exec` は `is_exec_available()` で gate される — 本物の sandbox backend
(= `"noop"` 以外) が configure されている場合のみ surface に出る。 残り
は常に visible。

`mcp` category は LLM に見える surface として 6 個の verb_object actions を提供する:

| Action | 用途 |
|---|---|
| `mcp__search_registry`  | 公式 MCP registry で新規 server を検索 |
| `mcp__install_registry` | registry の server を現 project に install |
| `mcp__install_package`  | npm / pypi / docker / github URL から install |
| `mcp__install_local`    | local command (LLM 生成 script 等) を直接登録 |
| `mcp__list_servers`     | install 済 server を列挙 |
| `mcp__list_tools`     | 1 server の tool を `<server>__<tool>` ID で列挙 |
| `mcp__call_tool`      | `<server>__<tool>` ID + `args` で tool を call |
| `mcp__drop_server`    | install 済 server を削除 |

## Qualified-name format

```
<category>__<entry_name>
```

separator は **double underscore** (`__`)。 category は `.` を含んでよく
(`agent.peer`, `rag.corpus`, `reyn.source` 等)、 entry name は boundary
の `__` sequence 以外なら任意。 split rule は 「category 名直後の最初の
`__`」 なので `agent.peer__alice` は (`agent.peer`, `alice`) と正しく
パースされる。

例:

| Qualified name | パース結果 |
|---|---|
| `skill__index_docs` | (`skill`, `index_docs`) |
| `agent.peer__alice` | (`agent.peer`, `alice`) |
| `mcp__call_tool` | (`mcp`, `call_tool`) |
| `mcp__install_registry` | (`mcp`, `install_registry`) |
| `rag.corpus__meetings` | (`rag.corpus`, `meetings`) |
| `file__read` | (`file`, `read`) |

### Provider portability — qualified name 中の `.`

OpenAI native function-call API は tool 名を `^[a-zA-Z0-9_-]{1,64}$` に
制限している (= `.` は不可)。 Reyn の qualified name はカテゴリに `.` を
含む形 (`agent.peer`, `rag.corpus`, `reyn.source` 等) があり、 **LiteLLM
proxy 経由なら OK** だが OpenAI native を直接叩く場合 reject される
可能性がある。

Reyn の標準設定は全 provider を LiteLLM 経由でルーティングする
(`reyn.yaml: models: standard: openai/...`) ため、 ドット入り名でも
end-to-end で動作する。 Gemini / Anthropic / OpenAI-compat endpoint は
すべて LiteLLM 経由なら `.` を許容する。

OpenAI native (= LiteLLM を介さない) 経路を新規に追加する場合は:

  - LiteLLM proxy を前に立てる (= 推奨、 Reyn の default に揃う)、 もしくは
  - qualified name を全て `_` ベースに移行 (= catalog enumerator /
    dispatch table / hot-list / fixture / scenario すべて同期 update が
    必要な breaking change。 FP-0034 §D18 で tracking)。

直接 OpenAI native callsite が現プロジェクトには存在しないため、
移行は今は scope 外。 LiteLLM proxy が canonical ingress。

## 3 つの wrapper

### `list_actions(category, filter, offset, limit) → {items, total}`

カタログをアルファベット順に browse する。 `category` は category 名の
list (省略 / `[]` 渡しで全 visible category)。 `filter` は
`qualified_name` と `short_description` に対する大文字小文字無視の部分
一致。 `offset` / `limit` で pagination。 各 item は `qualified_name` と
短い description を持つ; 長い description は意図的に出さず、 一覧を
コンパクトに保つ。

### `describe_action(action_name) → {qualified_name, description, input_schema, metadata}`

1 つの action の long description、 完全な input schema (= 元 tool の
`parameters`)、 metadata (`target_tool_name`, `category`, `purity`) を
返す。 未知の name に対しては §D12 の structured error response (下記
参照)。

### `invoke_action(action_name, args) → <target の result>`

routing layer (下記 [Dispatch](#dispatch-routing-layer) 参照) 経由で
target tool に dispatch する。 wrapper は transparent: target handler は
完全な `ToolContext` 下で動くので、 permission gate / events / budget /
workspace 効果は legacy tool を直接 call した場合と完全に同一。 未知の
name に対しては §D12 error response。

4 つ目の wrapper `search_actions` は semantic (embedding 基盤) 検索の
ために予約。 **Phase 1 では visible にしない** — handler は stub、
embedding 配管は Phase 2 待ち。

## Canonical-default 意味論 (§D19)

resource category は discover と invoke の両方を支援する。 resource を
invoke すると、 その種別の *canonical default operation* が走る:

| Resource category | Canonical default invoke |
|---|---|
| `skill` | skill を実行 |
| `agent.peer` | message を delegate |
| `memory.entry` | body を read |
| `rag.corpus` | single-source recall |

旧 `mcp.server` / `mcp.tool` の resource entry は削除。
per-MCP-server / per-MCP-tool dispatch は `mcp` category 内の verb
actions 経由 (= `mcp__list_tools` →
`mcp__call_tool({tool: "<server>__<tool>", args})`) で flow する。

これにより LLM は `invoke_action("rag.corpus__meetings", {"query": "Q3
roadmap"})` と書くだけで wrapper が `recall(sources=["meetings"],
query="Q3 roadmap")` に展開してくれる — 元 call の shape を LLM が記憶
する必要がない。 canonical default は `describe_action` の response に
明示される。

## Dispatch (routing layer)

qualified name → target tool 名のマッピングは
[`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn)
にある。 **pure** — I/O なし、 state なし、 live invocation なし。 2 つ
の table が routing を駆動する:

- **`_OPERATION_RULES`** — qualified name → `(target_tool_name,
  arg_transformer)`、 static operation category 向け (file / web /
  memory.operation / reyn.source / rag.operation / mcp)。
- **`_RESOURCE_RULES`** — category → `(target_tool_name,
  arg_transformer)`、 entry が `RouterCallerState` 由来の resource
  category 向け (skills / agents / memory entries / rag corpora)。

Routing は常に:

1. qualified name を (`category`, `entry_name`) に split。
2. その category / qualified name の rule を lookup。
3. arg transformer を実行 (例: `_invoke_skill_args` は caller の args を
   skill の input artifact に wrap)。
4. `ResolvedAction(target_tool_name, target_args)` を返し、 wrapper が
   それを unified `ToolRegistry` に渡す。

match する rule がなければ dispatch は `UnknownActionError` を raise
し、 既知の qualified name set + visible resource entries から
`difflib`-ranked suggestions を運ぶ。

## Error response (§D12)

`invoke_action` / `describe_action` が未知の `action_name` を受け取った
場合、 response は raise ではなく structured:

```json
{
  "error": "Unknown action 'skil__foo'",
  "reason": "...",
  "suggestions": ["skill__foo", "skill__form"],
  "hint": "Use list_actions(category=[...]) to discover the correct name."
}
```

`suggestions` は静的 qualified-name set と router-state-aware candidate
を merge し `difflib.get_close_matches` で生成。 hint は常に
`list_actions` に戻り、 LLM の recovery 手段を明示する。

## Visibility gating (§D14)

一部の category は runtime 環境で visibility-gate される:

| Predicate | 効果 |
|---|---|
| `is_search_available(embedding_class)` | `search_actions` が tools= に出るか (Phase 2) |
| `is_exec_available(sandbox_backend)` | `exec` が `list_actions` 列挙に出るか |

gate は pure function; runtime は `action_retrieval.embedding_class`
と resolved sandbox backend から configuration を渡す。 hidden category
は `list_actions` の `category=` enum にも列挙結果にも現れない。

## System prompt placement (§D9)

`action_retrieval.universal_wrappers_enabled` が true のとき、 router
system prompt に **`## Action categories`** section が加わり、 13
category と canonical-default 意味論を列挙する。 この section は
`## Capabilities` と `## Behaviour` の間に位置し、 static prompt-cache
prefix 内に留まる (= 2 回目以降の request は warm cache を hit)。

Tier 2 invariant が section の bullet 一覧を `CATEGORIES` tuple に
pin しているので、 master taxonomy への将来の追加が SP と乖離する場合は
test が落ちる。

## Default-on (PR-3b-iv)

production では `ActionRetrievalConfig.universal_wrappers_enabled` の
default は `True`。 `build_tools` / `build_system_prompt` を直接呼ぶ
caller (= `FakeRouterHost` を組む unit test fixture 等) で
`ActionRetrievalConfig` を渡さないものは引き続き legacy off behavior に
留まる。 これは `RouterLoop` が `getattr(host,
"get_universal_wrappers_enabled", None)` fallback で flag を読むため
で、 method が無い場合は `False` 扱い。 この dual path によって
LLMReplay fixture は byte-valid のまま、 production router は新 tools
を得る。

opt-out したい場合は `reyn.yaml` に:

```yaml
action_retrieval:
  universal_wrappers_enabled: false
```

## Phase 1 から外れているもの

構造的 surface は完成; behavioral / discovery 系は Phase 2 へ:

- **`search_actions`** — semantic, embedding 基盤の検索。 handler は
  stub、 visibility は `ActionEmbeddingIndex` 待ち。
- **`rag.corpus` 列挙** — `RouterCallerState` に indexed-source の
  metadata を運ぶ field 追加 + `RouterHostAdapter` 経由の配管が必要。
  `invoke` と `describe` の path は `rag.corpus__<name>` を LLM が知って
  いれば動く。
- **`exec` 列挙** — sandbox-backend introspection が必要。 visibility
  predicate は存在; カタログ本体は introspection API 待ち。
- **Hot-list pinning** — `action_retrieval.hot_list_n` はパースされる
  が未使用; Phase 2 で `list_actions` の順位を直近 invoke の action 寄り
  に bias するために利用。

## 参照ファイル

- [`src/reyn/tools/universal_catalog.py`](https://github.com/anthropics/reyn) — `CATEGORIES`、 4 ToolDefinition、 qualified-name parser、 D14 helper、 real handler
- [`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn) — routing table、 `ResolvedAction`、 `UnknownActionError`、 `suggest_similar_names`
- [`src/reyn/chat/router_tools.py`](https://github.com/anthropics/reyn) — `build_tools` integration (flag-gate された wrapper)
- [`src/reyn/chat/router_system_prompt.py`](https://github.com/anthropics/reyn) — `## Action categories` section
- [`src/reyn/config.py`](https://github.com/anthropics/reyn) — `ActionRetrievalConfig`
- [`docs/reference/config/reyn-yaml.ja.md`](../reference/config/reyn-yaml.ja.md#action_retrieval) — config reference
