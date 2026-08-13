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
wrapper** に置き換える。 あらゆる action — workflow / peer agent / MCP
tool / memory entry / file op / indexed corpus / … — は **1 つの名前**
でアドレッシングされ、 `invoke_action` 経由
で dispatch される。 discovery は `list_actions`、 詳細 introspection は
`describe_action`、 自然言語 / semantic 検索は `search_actions`
(embedding-backed) で扱う。

**状態の更新: ツール提示は今や pluggable な scheme であり、単一の固定パスではありません。** Phase 6 (2026-05-16) 以降、wrapper-only path は一時的に唯一の production 挙動でしたが、後に owner による H1 fix が `chat` レイヤー自身のデフォルトを `enumerate-all`(wrapper なしの flat なツールリスト)に切り替えました — flat listing が `invoke_action` の name-hallucination を防ぐためです(30%→100% の direct tool-use 精度)。`universal-category`(このページの wrapper path)は登録済み scheme として残存し、operator が `reyn.yaml` で `tool_use.scheme: category` を設定すれば到達できます(FP-0066 P4b #3247 — presentation 軸の名前は `category`、解決先の登録済み scheme 名が `universal-category`)。完全で現行のモデルは [Tool-Use Schemes](tool-use-schemes.md) を参照してください。以下のセクションは `universal-category` scheme 自体の仕組みを説明するものであり、どのレイヤーがそれをデフォルトで使うかではありません。(#2768 が死んだ phase-graph era の `step`/`phase` tool-use レイヤーを削除しました。)

この wrapper path があるレイヤーで有効なとき、handler
(`invoke_skill` / `call_mcp_tool` / …) は wrapper
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
| `skill` | project / stdlib workflow | `input` artifact を持ってワークフローを実行 |
| `agent.peer` | topology 内の peer agent | その peer にメッセージを delegate |
| `mcp` | MCP server 管理 + tool dispatch | 6 個の verb_object actions — 下表参照 |
| `file` | workspace の file op | read / write / delete / list |
| `web` | web search + fetch | search または fetch |
| `memory_operation` | memory op | `list` / `read` (`layer` + `slug` 指定) / `remember_shared` / `remember_agent` / `forget` |
| `reyn_repo` | Reyn source / docs (read-only) | read または list |
| `rag_operation` | RAG op | `list_sources` / multi-source semantic_search / drop source |
| `exec` | sandboxed argv 実行 | sandbox backend 下で argv 実行 |

`exec` は `is_exec_available()` で gate される — 本物の sandbox backend
(= `"noop"` 以外) が configure されている場合のみ surface に出る。 残り
は常に visible。

**どの category も列挙する action は固定 verb 集合**。 resource (保存済み
memory / indexed corpus / install 済 MCP tool / 登録済 pipeline) は verb の
**引数** であって、それ自体が列挙される action ではない — だから LLM に見せる
action 数は operator が溜め込んだ量に依存しない。 resource category の collapse で
resource を *名指しする* surface が失われる箇所には、固定数の discovery verb を
置いてある (`list_memory` / `list_mcp_tools` / `pipeline_list` /
`skill_list`)。

`mcp` category は LLM に見える surface として 6 個の verb_object actions を提供する:

| Action | 用途 |
|---|---|
| `mcp_search_registry`  | 公式 MCP registry で新規 server を検索 |
| `mcp_install_registry` | registry の server を現 project に install |
| `mcp_install_package`  | npm / pypi / docker / github URL から install |
| `mcp_install_local`    | local command (LLM 生成 script 等) を直接登録 |
| `list_mcp_servers`     | install 済 server を列挙 |
| `list_mcp_tools`     | 1 server の tool を `<server>__<tool>` ID で列挙 |
| `mcp_call_tool`      | `<server>__<tool>` ID + `tool_args` で tool を call |
| `mcp_drop_server`    | install 済 server を削除 |

## Action name (#3429)

action の名前は **flat な registry tool 名** — `read_file` / `web_search` /
`mcp_call_tool` — で、 それが唯一の名前。 category は browsing 軸
(`list_actions(category=["file"])`) であって、 名前の一部ではない。

**§D18 はかつて 2 つ目の綴りを規定していた** — `<category>__<verb>` (=
`read_file` は `file__read` でもあった) — parser と、 一方を他方に写す
routing table つきで。 1 つの操作に 2 つの名前があると、 **tool 名で引く
subsystem ごとに「両形に対応するか忘れるか」の賭けが 1 回発生する**。 実在
する 11 subsystem を数えたところ、 明示的な両形補償があるのは 4 (permission
軸の `_expand_tool_forms`、 op-gate の alias 表、…)、 無いのが 7 (結果の正規化 /
canonicalization の宣言 / permission-denied のヒント / 広告ゲート /
exclusive-wrapper の strip 一覧 / `routing_decided` の audit-event /
action-usage 追跡)。 7 を直しても 12 番目の subsystem が同じ賭けをするので、
**2 つ目の名前の方を消した**。

生き残った名前の命名規約は
[`docs/reference/runtime/tool-naming.md`](../../reference/runtime/tool-naming.md)。
gate は `tests/tools/test_no_qualified_tool_names_3429.py` — live registry /
membership table / categories / 組み立て済み `tools=` payload を回り、 名前に
`__` があれば落ちる。 (削除は状態、 gate が性質。)

副次的効果として、 全ての名前が OpenAI native の function-name 文法
`^[a-zA-Z0-9_-]{1,64}$` を構成上満たす。 qualified name を LiteLLM proxy 依存
にしていたドット入り category はとうに無く、
`tests/tools/test_qualified_name_provider_grammar_1456.py` がこの性質を pin している。

## 3 つの wrapper

### `list_actions(category, filter, offset, limit) → {items, total}`

カタログをアルファベット順に browse する。 `category` は category 名の
list (省略 / `[]` 渡しで全 visible category)。 `filter` は
`action_name` と `short_description` に対する大文字小文字無視の部分
一致。 `offset` / `limit` で pagination。 各 item は `action_name` と
短い description を持つ; 長い description は意図的に出さず、 一覧を
コンパクトに保つ。

**weak-model landing 設計** では、 category で絞った結果は各 item の
完全な `description` と `input_schema` を運ぶ (= `action_name` +
`description` + `input_schema` の3点)。 これにより common flow は
`list_actions` → `invoke_action` の2段になり、 間の `describe_action`
が不要になる。
[Weak-model discovery + selection reliability](#weak-model-discovery-selection-reliability)
参照。

### `describe_action(action_name) → {action_name, description, input_schema, metadata}`

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

membership table (下記 [Membership](#membership-action-名の意味) 参照) 経由で
target tool に dispatch する。 wrapper は transparent: target handler は
完全な `ToolContext` 下で動くので、 permission gate / events / budget /
workspace 効果は legacy tool を直接 call した場合と完全に同一。 未知の
name に対しては §D12 error response。

4 つ目の wrapper `search_actions` は semantic (embedding 基盤) 検索の
ために予約。 **Phase 1 では visible にしない** — handler は stub、
embedding 配管は Phase 2 待ち。

## Resource 名: 列挙 (enumeration) と解決 (resolution) (§D19)

**列挙** は LLM に *見せる* もの、 **解決** は caller が既に書いた name が
*何をするか*。 この 2 つは意図的に別 surface であり、 payload size を決める
のは列挙のみ。

resource は 1 つも列挙されない。 到達するには、 category の discovery verb で
discover し、 その id を引数として渡す:

| やりたいこと | discover | invoke |
|---|---|---|
| 保存済 memory を read | `list_memory` | `read_memory_body({layer: "shared", slug: "..."})` |
| MCP tool を call | `list_mcp_tools` | `mcp_call_tool({tool: "<server>__<tool>", tool_args})` |
| 登録済 pipeline を実行 | `pipeline_list` | `run_pipeline({name: "greet", input: {...}})` |
| 自分の knowledge を検索 | — | `search_knowledge({query: "..."})` |

`read_memory_body` は `layer` (`shared` または `agent`) を明示的に取るので、
両 layer とも catalog から read できる。

MCP の行にある `<server>__<tool>` は MCP **サーバ側** の tool 識別子であり、
Reyn が所有しない namespace の引数値。 Reyn の tool 名ではない。

**#3429 が author-time の例外を削除した。** 「caller が既に書いた name の解決
は tool を 1 つも消費しない」という理由で、 列挙されないまま **解決だけは
続いていた** resource 形式が 2 つあった: `pipeline__<name>` (pipeline guide
が教えていた形) と `mcp__<server>__<tool>` (pipeline DSL の `tool:` step)。
この理由は payload については真だが、 命名については何も言っていない — どちらも
既存 verb の **2 つ目の名前**であり、 2 つ目の名前こそが「tool 名で引く
subsystem ごとの賭け」の源。 pipeline step は flat な tool を名指し、 resource
id は上表のとおり通常の引数として渡す。

## Membership (action 名の意味)

**action** は登録済みの `ToolDefinition` で、 flat な registry 名でアドレッシング
される。 **category** はその集合に対する browsing 軸。 membership table は
[`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn)
にあり **pure** — I/O なし、 state なし、 live invocation なし:

- **`_CATEGORY_ACTIONS`** — category → その category が browse する flat tool 名
  の **閉じた表**。 enumerator が読んでよい唯一の表 — payload を constant に
  保っているのはこの点。

`invoke_action` は:

1. `action_name` を `KNOWN_ACTION_NAMES` に照合 (`require_known_action`)。
2. その名前を unified `ToolRegistry` で lookup。
3. その tool 自身の handler を、 caller が送った args のまま呼ぶ。

**(1) と (3) の間に書き換え段は無い。** #3429 までは有った: 名前は
`<category>__<verb>` 綴りで届き、 この層が flat な registry 名に写していた。
しかも 2 つの写像は args も組み替えていた (`cluster`→`path`、
`message`→`request`) — **どの広告スキーマにも宣言されていない引数**、 つまり
qualified 経路だけが持っていた能力。 model が送った args がそのまま handler が
受け取る args であることが、 "transparent wrapper" が本来意味していたこと。

action でない名前なら dispatch は `UnknownActionError` を raise し、 live で
availability を反映した action 集合から `difflib`-ranked suggestions を運ぶ。

## Error response (§D12)

`invoke_action` / `describe_action` が未知の `action_name` を受け取った
場合、 response は raise ではなく structured:

```json
{
  "error": "Unknown action 'read_fil'",
  "reason": "not a known action name",
  "suggestions": ["read_file", "edit_file", "delete_file"],
  "hint": "Use list_actions(category=[...]) to discover the correct name."
}
```

`suggestions` は live で availability を反映した action 集合に対して
`difflib.get_close_matches` で生成。 hint は常に
`list_actions` に戻り、 LLM の recovery 手段を明示する。

## Visibility gating (§D14)

一部の category は runtime 環境で visibility-gate される:

| Predicate | 効果 |
|---|---|
| `is_search_available(embedding_enabled)` | `search_actions` が tools= に出るか (Phase 2) |
| `is_exec_available(sandbox_backend)` | `exec` が `list_actions` 列挙に出るか |

gate は pure function; runtime は `embedding.enabled`(FP-0066 §7)
と resolved sandbox backend から configuration を渡す。 hidden category
は `list_actions` の `category=` enum にも列挙結果にも現れない。

## System prompt placement (§D9)

`action_retrieval.universal_wrappers_enabled` が true のとき、 router
system prompt に **`## Action categories`** section が加わり、 全
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

1. **Satisficing** — より適した action (`edit_file`) を discover せず、
   馴染みの action (`write_file`) を「十分」として invoke する。
2. **Discovery-skip** — 能動的に `list_actions` を呼ばず、 training prior
   から action 名を推測する (しばしば malformed: `file.write`,
   `file__read_file`)。

*Status: no-names system prompt と `edit_file` cross-reference は出荷済;
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
  obscure なワークフローで `list_actions` 16/16 を観測)。

  注意 — 名前隠蔽が discovery を強制するのは *未知* action のみ。 training
  で **既知** の概念 (`read_file` / `write_file`) では、 weak model は
  概念を recall し、 正確な名前を discover せず malformed な近似を emit
  する。 既知 action の *選択* は名前隠蔽ではなく、 下記の機械的 mandate
  で扱う。

### `list_actions` が name + description + schema を返す

`list_actions(category=[…])` が bounded set に絞ったとき、 各 item は
**3点セット** — `action_name` / `description` / `input_schema` — を運ぶ:

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
`edit_file` を推奨」) は無視され (0/20 が従う)、 無条件 mandate (「edit は
`write_file` でなく `edit_file` を使わ MUST」) は従われる (edit 3 /
write 1)。

そのため router は一連の機械的 system-prompt mandate を model tier で
gate する (`router_model: light` → on; `strong` → off):

- **`list_actions`-first** — 最初の tool 呼び出しは、 何かを read / write
  / edit する前に MUST `list_actions`。
- **`edit_file`-MUST** — partial / surgical edit は `write_file` でなく
  `edit_file` を使う。

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

**このセクションは `universal_wrappers_enabled` フラグ自身のデフォルトを説明するものであり、今日どの tool-use scheme がそれに解決されるかではありません** — このページ冒頭の状態更新を参照してください: `tool_use.scheme`(x `tool_use.transport`)selector がこのフラグの *選択* 役割を generalize しており、chat レイヤー自身の scheme デフォルト(`enumerate-all`)はこのフラグを一切経由しません。フラグ自体は `universal-category` scheme の live な presentation(catalog-wrapper vs direct-tool)として残存します。

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
- **`exec` 列挙** — sandbox-backend introspection が必要。 visibility
  predicate は存在; カタログ本体は introspection API 待ち。

**廃止済み（#4552、2026-08）:** hot-list 機構（`action_retrieval.hot_list_n`、
top-N freq+recency の direct-alias 投影、デフォルト無効）がここに存在して
いたが、削除された — owner 指示: 機構の役割はすでに無くなっており、
`list_actions` が正規の discovery path として代替済み。

## 参照ファイル

- [`src/reyn/tools/universal_catalog.py`](https://github.com/anthropics/reyn) — `CATEGORIES`、 4 ToolDefinition、 D14 helper、 real handler
- [`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn) — `_CATEGORY_ACTIONS` membership table、 `require_known_action`、 `UnknownActionError`、 `suggest_similar_names`
- [`src/reyn/runtime/router_tools.py`](https://github.com/anthropics/reyn) — `build_tools` integration (flag-gate された wrapper)
- [`src/reyn/runtime/router_system_prompt.py`](https://github.com/anthropics/reyn) — `## Action categories` section
- [`src/reyn/config/embedding.py`](https://github.com/anthropics/reyn) — `ActionRetrievalConfig`
- [`docs/reference/config/reyn-yaml.ja.md`](../../reference/config/reyn-yaml.ja.md#action_retrieval-ブロック) — config reference
