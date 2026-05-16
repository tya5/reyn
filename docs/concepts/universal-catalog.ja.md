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
describe / invoke tool を、 **全 category を一律にカバーする 3 つの
wrapper** に置き換える。 あらゆる action — skill / peer agent / MCP
tool / memory entry / file op / indexed corpus / … — は単一の qualified
name (`<category>__<entry>`) でアドレッシングされ、 `invoke_action` 経由
で dispatch される。 discovery は `list_actions`、 詳細 introspection は
`describe_action` で扱う。

3 つの wrapper は [`reyn.yaml`](../reference/config/reyn-yaml.ja.md#action_retrieval)
の `action_retrieval` で flag-gate され、 default で ON。 既存の
LLMReplay fixture は引き続き有効: legacy tool (`invoke_skill` /
`delegate_to_agent` / `call_mcp_tool` / …) は残り、 wrapper は単にそれら
経由で再 routing するだけ。 handler は 1 つも再実装していない、 アドレ
ッシング層が増えただけ。

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

## 13 category (§D18 master taxonomy)

| Category | 保持するもの | Canonical invoke 意味論 |
|---|---|---|
| `skill` | project / stdlib skill | `input` artifact を持って skill を実行 |
| `agent.peer` | topology 内の peer agent | その peer にメッセージを delegate |
| `mcp.server` | 設定済み MCP server (resource) | この server の tools を列挙 |
| `mcp.tool` | 各 server の個別 tool | `args` を持って tool を call |
| `mcp.operation` | MCP server 管理 op | op を実行 (例: `drop_server`) |
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

## Qualified-name format

```
<category>__<entry_name>
```

separator は **double underscore** (`__`)。 category は `.` を含んでよく
(`mcp.tool`)、 entry name は boundary の `__` sequence 以外なら任意。
split rule は 「category 名直後の最初の `__`」 なので
`mcp.tool__brave.search` は (`mcp.tool`, `brave.search`) と正しくパース
される。

例:

| Qualified name | パース結果 |
|---|---|
| `skill__code_review` | (`skill`, `code_review`) |
| `agent.peer__alice` | (`agent.peer`, `alice`) |
| `mcp.tool__brave.search` | (`mcp.tool`, `brave.search`) |
| `mcp.operation__drop_server` | (`mcp.operation`, `drop_server`) |
| `rag.corpus__meetings` | (`rag.corpus`, `meetings`) |
| `file__read` | (`file`, `read`) |

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
| `mcp.server` | この server の tools を列挙 |
| `mcp.tool` | tool を call |
| `memory.entry` | body を read |
| `rag.corpus` | single-source recall |

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
  memory.operation / reyn.source / rag.operation / mcp.operation)。
- **`_RESOURCE_RULES`** — category → `(target_tool_name,
  arg_transformer)`、 entry が `RouterCallerState` 由来の resource
  category 向け (skills / agents / mcp servers / mcp tools / memory
  entries / rag corpora)。

Routing は常に:

1. qualified name を (`category`, `entry_name`) に split。
2. その category / qualified name の rule を lookup。
3. arg transformer を実行 (例: `_call_mcp_tool_args` は `entry_name`
   を `(server, tool)` に分け、 `args` を pack)。
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
