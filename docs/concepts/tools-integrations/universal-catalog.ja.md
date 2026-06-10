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
| `memory_entry` | 永続 memory record | entry の body を read |
| `memory_operation` | memory CRUD op | `remember_shared` / `remember_agent` / `forget` |
| `reyn_source` | Reyn source / docs (read-only) | read または list |
| `rag_corpus` | indexed corpora (resource) | この single source に対して recall |
| `rag_operation` | RAG 管理 op | multi-source recall / drop source |
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
(`agent.peer`, `rag_corpus`, `reyn_source` 等)、 entry name は boundary
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
| `rag_corpus__meetings` | (`rag_corpus`, `meetings`) |
| `file__read` | (`file`, `read`) |

### Provider portability — qualified name 中の `.`

OpenAI native function-call API は tool 名を `^[a-zA-Z0-9_-]{1,64}$` に
制限している (= `.` は不可)。 Reyn の qualified name はカテゴリに `.` を
含む形 (`agent.peer`, `rag_corpus`, `reyn_source` 等) があり、 **LiteLLM
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

**weak-model landing 設計** では、 category で絞った結果は各 item の
完全な `description` と `input_schema` を運ぶ (= `qualified_name` +
`description` + `input_schema` の3点)。 これにより common flow は
`list_actions` → `invoke_action` の2段になり、 間の `describe_action`
が不要になる。
[Weak-model discovery + selection reliability](#weak-model-discovery-selection-reliability)
参照。

### `describe_action(action_name) → {qualified_name, description, input_schema, metadata}`

1 つの action の long description、 完全な input schema (= 元 tool の
`parameters`)、 metadata (`target_tool_name`, `category`, `purity`) を
返す。 未知の name に対しては §D12 の structured error response (下記
参照)。

weak-model landing 設計では `describe_action` は **common critical path
から外れる** — `list_actions` が絞った category について description +
schema を既に返すため。 edge case 用にのみ残す: 単一名 lookup、 もしくは
全 schema を list 結果に inline すると無駄になるほど大きい category。
[Weak-model discovery + selection reliability](#weak-model-discovery-selection-reliability)
参照。

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
| `memory_entry` | body を read |
| `rag_corpus` | single-source recall |

旧 `mcp.server` / `mcp.tool` の resource entry は削除。
per-MCP-server / per-MCP-tool dispatch は `mcp` category 内の verb
actions 経由 (= `mcp__list_tools` →
`mcp__call_tool({tool: "<server>__<tool>", args})`) で flow する。

これにより LLM は `invoke_action("rag_corpus__meetings", {"query": "Q3
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
  memory_operation / reyn_source / rag_operation / mcp)。
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

## Weak-model discovery + selection reliability

discover→invoke loop は LLM がそれを *使う* 意志の分だけしか機能しない。
strong model (`router_model: strong`) は category 一覧から action を柔軟
に discover / select でき、 追加の足場は要らない。 weak / small model
(`router_model: light`) は 2 つの信頼できる failure mode を示し、 catalog
はこれを **構造的に** 解く — weak 対応が strong の柔軟性を損なわない形で:

1. **Satisficing** — より適した action (`file__edit`) を discover せず、
   見えている hot-list action (`file__write`) を「十分」として invoke する。
2. **Discovery-skip** — 能動的に `list_actions` を呼ばず、 training prior
   から action 名を推測する (しばしば malformed: `file.write`,
   `file__read_file`)。

*Status: no-names system prompt と `file__edit` cross-reference は出荷済;
`list_actions` が schema を返す点と tier-gated mandate は合意済の landing
設計 (実装進行中)。 以下の各 lever は `gemini-2.5-flash-lite` に対し
patch + live で reliable N で検証済。*

### No-names catalog

action 名は **ただ 1 箇所** — `list_actions` の結果 — にのみ現れる。
system prompt (category を capability で記述し、 action 名は載せない) や
他のあらゆる tool の description には存在しない。 これは 2 つの目的に資する:

- **Scalability** — LLM 可視の tool 一覧と system prompt が action 数に
  対し O(1) に保たれる; 200-action surface でも 20-action と同じ prompt
  コスト。
- **真に未知の action の強制 discovery** — 名前が model の記憶し得る
  どこにも存在しないとき、 それを得る唯一の手段は `list_actions` の呼び
  出しになる。 真に未知の action ではこれが確実に fire する (非推測な
  obscure skill で `list_actions` 16/16 を観測)。

  注意 — 名前隠蔽が discovery を強制するのは *未知* action のみ。 training
  で **既知** の概念 (`file__read` / `file__write`) では、 weak model は
  概念を recall し、 正確な名前を discover せず malformed な近似を emit
  する。 既知 action の *選択* は名前隠蔽ではなく、 下記の機械的 mandate
  で扱う。

### `list_actions` が name + description + schema を返す

`list_actions(category=[…])` が bounded set に絞ったとき、 各 item は
**3点セット** — `qualified_name` / `description` / `input_schema` — を運ぶ:

- **`description`** は model が正しい action を *選ぶ* ための材料; model は
  読めない action を選べない (tool description の慣例的役割)。
- **`input_schema`** は選んだ action を正しい引数で *invoke* するための形。

絞った結果が両方を運ぶため、 common flow は **2 段 — `list_actions` →
`invoke_action`** — で、 間に `describe_action` を挟まない。 コンパクト
さは *category-narrowing* (聞いた category の schema だけ来る) で保たれ、
schema を全体的に省くことでは保たない。

検証 (schema → invocation 軸): `list_actions` 結果に schema を注入すると、
受動的な `describe_action` 呼び出しが 14→0、 引数正答が 0→12 (/20) に
なった — list に schema があれば weak model は別の describe round-trip
なしに正しく invoke する。 description → selection 軸は tool description
の慣例的役割 (読めない action は選べない) であり、 description は別途
測定した lever ではなく設計根拠として運ぶ。

### 機械的 mandate (tier-gated)

weak model は **機械的・無条件の手続き mandate には従う** が **推論ベース
の推奨は無視する**。 *説明する* cross-reference (「partial edit には
`file__edit` を推奨」) は無視され (0/20 が従う)、 無条件 mandate (「edit は
`file__write` でなく `file__edit` を使わ MUST」) は従われる (edit 3 /
write 1)。

そのため router は一連の機械的 system-prompt mandate を model tier で
gate する (`router_model: light` → on; `strong` → off):

- **`list_actions`-first** — 最初の tool 呼び出しは、 何かを read / write
  / edit する前に MUST `list_actions`。
- **`file__edit`-MUST** — partial / surgical edit は `file__write` でなく
  `file__edit` を使う。

mandate を効かせるのは 2 つの性質:

1. **明示的 action 列挙の wording。** mandate が covers する具体操作を
   名指す (「read / write / edit する前に」) と 25-55% compliance; 一般的
   表現 (「他の tool の前に」) は 0-10%。
2. **Constraint reinforcement。** mandate を system prompt 中に ~3× 反復
   すると compliance が ~36% から **~75-85%** に上がる (matched-pair で
   検証、 distribution overlap なし)。 反復は small model が推論途中で
   指示を取りこぼす goal-displacement に対抗する。

### 天井

明示列挙 wording + 3× reinforcement で `list_actions`-first mandate は
**~75-85% の weak-model compliance** に達する。 これが実用的な prompting
天井: 残り ~15-25% は prompting だけでは閉じない alignment fragility で、
さらに狭めるには fine-tuning が要り scope 外。 strong model は mandate を
off で走り影響を受けない。

### 統一原理

> weak model は **真に未知の** action を **自力 discover** し、 **機械的
> mandate に従う**; **training 既知** の名前では **recall して flail** し、
> **推論ベースの推奨を無視する**。 ゆえに catalog は名前を隠し (未知
> discovery を強制)、 description + schema を絞った list に載せ (describe
> round-trip を除去)、 機械的 mandate を weak tier で gate する (既知
> action の選択を解く) — strong model は無制約のまま。

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
- **`rag_corpus` 列挙** — `RouterCallerState` に indexed-source の
  metadata を運ぶ field 追加 + `RouterHostAdapter` 経由の配管が必要。
  `invoke` と `describe` の path は `rag_corpus__<name>` を LLM が知って
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
- [`docs/reference/config/reyn-yaml.ja.md`](../../reference/config/reyn-yaml.ja.md#action_retrieval) — config reference
