---
type: concept
topic: architecture
audience: [human, agent]
---

# LLM invocation surfaces — Router-style vs Phase-style

## 1. なぜこれが重要か

Reyn は LLM を2つの異なる文脈で呼び出す。チャットルーター（および plan-mode バリアント）と、Skill 内部の Phase executor である。各文脈はそれぞれ固有の機能語彙を持つ — ルーターには function calling tools、Phase には Control IR ops。この2つのセットは大きく重なるが、完全には一致しない。この乖離を明文化しなければ、貢献者は便宜上どちらかの surface に新機能を追加し続け、差異は静かに広がる。本ドキュメントは2つの invocation kind を命名し、乖離を整理し、どの非対称性に原則的根拠があるか・どれが convention drift かを識別する。これにより、将来の追加が正しい場所に着地し、意図しない非対称性が積み重なる前に表面化するようになる。

---

## 2. 2つの invocation kind

### 2.1 Router-style（チャット・planner）

**使用箇所:** `RouterLoop`（インタラクティブチャットセッション）および `PlanRuntime`（plan-mode ステップ実行）。両者は単一実装を共有する — コンテキストごとにカタログを絞り込む `RouterLoopHost` facade を用いた `RouterLoop`。

**仕組み:** litellm 経由の `call_llm_tools` によるネイティブ LLM function calling。ツール定義は OpenAI `tools` 配列形式に従い、モデルはアシスタントメッセージ内の `tool_calls` で応答する。OS は各呼び出しをディスパッチし、`tool_result` を追記して、モデルが通常テキストを返すまで LLM を再呼び出しする。

**ツール surface:** `src/reyn/chat/router_tools.py` の `build_tools()` がツールリストを組み立てる。実際の数はオペレーター設定に依存する。

- **常時存在（14 tools）:** `list_skills`, `describe_skill`, `list_agents`, `describe_agent`, `list_memory`, `read_memory_body`, `delegate_to_agent`, `remember_shared`, `remember_agent`, `forget_memory`, `web_search`, `plan`, `reyn_src_list`, `reyn_src_read`
- **条件付き（+0〜+9 tools）:** `invoke_skill`（Skill 登録時）、`list_directory` + `read_file`（file read スコープ設定時）、`write_file` + `delete_file`（file write スコープ設定時）、`web_fetch`（オペレーターのオプトイン）、`list_mcp_servers` + `list_mcp_tools` + `call_mcp_tool`（MCP servers 設定時）
- **実測レンジ: 14–23 tools**（`router_tools.py` のコメントに記載の「11–18」は `web_search`、`plan`、`reyn_src_list`、`reyn_src_read` 追加前の記述であり、現在は stale）

**Plan-mode は同一 surface から `plan` 自身を除いたもの:** `PlanRuntime` は `execute_plan` をラップし、`exclude_tools={"plan"}` を指定した `RouterLoop` を内部で使用する。これにより再帰的な plan 分解を防ぐ。親セッションで使用可能な他のすべての router tool は各 plan step で利用可能であり、step の `tools` リストでさらに絞り込まれる。

**役割:** オーケストレーション — 次のサブコンポーネント（Skill、Agent、plan、メモリ操作、直接テキスト応答）を選択する。

### 2.2 Phase-style（Skill 実行）

**使用箇所:** `OSRuntime` が駆動する、Skill 内すべての Phase 呼び出し。

**仕組み:** JSON output contract。LLM は単一の構造化応答を返す。

```json
{
  "control": {"type": "transition|finish|abort", "decision": "continue|finish|abort",
               "next_phase": "<name> or null", "confidence": 0.0, "reason": {}},
  "artifact": {"type": "<schema_name>", "data": {}},
  "control_ir": []
}
```

ネイティブ function calling はない。LLM は意図するサイドエフェクトを型付き op オブジェクトとして `control_ir` に宣言し、OS がそれらをディスパッチする。

**Op surface:** `src/reyn/op_runtime/registry.py` の `OP_KIND_MODEL_MAP` で定義された 8 種類の Control IR op kind。

| Op kind | 目的 |
|---------|------|
| `file` | ファイルの読み取り・書き込み・glob・grep・編集・削除 |
| `mcp` | 設定済み MCP server のツールを呼び出す |
| `run_skill` | 別の Skill をネストされたワークフローとして呼び出す |
| `shell` | シェルコマンドを実行する |
| `lint` | Skill ディレクトリに DSL linter を実行する |
| `ask_user` | Phase を一時停止してユーザーに入力を求める |
| `web_fetch` | 単一 URL を取得する |
| `web_search` | 公開ウェブを検索する |

各 Phase はさらに Phase 宣言の `allowed_ops: list[str]`（デフォルト: `["file", "ask_user"]`）によってこのセットを絞り込む。OS は defense-in-depth として `allowed_ops` をディスパッチ時に強制する。

**役割:** ドメイン作業 — 次の Phase または Skill の最終出力として artifact を生成する。

### 2.3 第3の invocation kind ではないもの

2つのコンストラクトは同じ Phase 実行コンテキストに登場するため、LLM invocation kind と混同されることがある。

**Preprocessor steps**（`run_skill` / `iterate` / `validate` / `lint_plan` / `python`）は Phase LLM 呼び出しの前に決定論的に実行される。それ自体は LLM を呼び出さない。`python` ステップはサンドボックス化された Python 関数を実行する。`run_skill` ステップは sub-skill を再帰的にディスパッチし、その sub-skill には Phase-style で LLM を呼び出す Phase が含まれるが、preprocessor ステップ自体は同期的な OS 制御であり、preprocessing 層から LLM 呼び出しは行わない。[preprocessor.md](preprocessor.md) 参照。

**Postprocessor steps**（同一 step types）は LLM の `finish` 出力の後、artifact が呼び出し元に返される前に決定論的に実行される。LLM 呼び出しではない。[postprocessor.md](postprocessor.md) 参照。

両者はOS が実行する決定論的パイプラインであり、LLM invocation ではない。

---

## 3. 機能比較マトリックス

| 機能 | Router-style surface | Phase-style surface | 状態 |
|------|---------------------|---------------------|------|
| ファイル読み取り | `read_file`（file read 権限付与時） | `file` op (op=read) | Symmetric |
| ファイル書き込み・削除 | `write_file`, `delete_file`（file write 権限付与時） | `file` op (op=write/delete) | Symmetric |
| ディレクトリ一覧 | `list_directory` | `file` op (op=glob) | Symmetric |
| Web 検索 | `web_search`（常時） | `web_search` op | Symmetric |
| Web フェッチ | `web_fetch`（オペレーターのオプトイン） | `web_fetch` op | Symmetric |
| MCP call_tool | `call_mcp_tool`（mcp_servers 設定時） | `mcp` op | Symmetric |
| MCP 検索（サーバー・ツール一覧） | `list_mcp_servers`, `list_mcp_tools` | 利用不可 | Gap (Type C) |
| Shell | 利用不可 | `shell` op | Role-separated (Type B) |
| Lint | 利用不可 | `lint` op | Role-separated (Type B) |
| Skill の実行・呼び出し | `invoke_skill`（Skill 登録時） | `run_skill` op | Symmetric |
| Agent 間委任 | `delegate_to_agent` | 利用不可 | Role-separated (Type B) |
| ユーザーへの質問 | ツールとしては存在しない（router はテキスト応答で終了） | `ask_user` op | Role-separated (Type B) |
| メモリ読み取り | `list_memory`, `read_memory_body` | context_builder による注入のみ（Phase 開始時のスナップショット） | Gap (Type C) |
| メモリ書き込み | `remember_shared`, `remember_agent`, `forget_memory` | 利用不可 | Gap (Type C) |
| カタログ閲覧 | `list_skills`, `describe_skill`, `list_agents`, `describe_agent` | ContextFrame の `op_catalog` 注入のみ（mid-phase クエリ不可） | Gap (Type C) |
| Plan 呼び出し | `plan` | 利用不可（Phase 内分解には `run_skill` を使用） | Role-separated (Type B) |
| Reyn ソース読み取り | `reyn_src_list`, `reyn_src_read` | 利用不可 | Router-only |

---

## 4. 4つの乖離タイプ

### Type A — 健全な対称性

両側に同一のセマンティクスで存在するが、呼び出し形式が異なる機能（function calling vs Control IR JSON）。これらは問題ではなく、2つの API スタイルから生じる自然な結果である。

**例:** file ops（`read_file` ↔ `file/read`）、web ops（`web_search` / `web_fetch` ↔ `web_search` / `web_fetch` ops）、MCP 呼び出し（`call_mcp_tool` ↔ `mcp` op）、Skill 呼び出し（`invoke_skill` ↔ `run_skill` op）

router LLM は `invoke_skill("name", input={...})` を呼び出し、Phase LLM は `{"kind": "run_skill", "skill": "name", "input": {...}}` を emit する。OS は両方をディスパッチする。対称性は実在しており、surface 形式が異なるのは2つの invocation kind が異なるプロトコルを使用しているためである。

### Type B — 意図的な役割分離

原則的な理由で存在し、非対称のままであるべき非対称性。

- **`delegate_to_agent` は router-only。** Phase は Skill スコープ内で動作する。ピア Agent へのリクエストルーティングはチャットセッションに属するオーケストレーション上の決定であり、Phase 実行途中のものではない。Phase 内部から agent 委任を許可することは、オーケストレーション層（セッション）とドメイン作業層（Phase）を混同させる。

- **`plan` は router-only。** Phase には in-phase 分解のための `run_skill` がすでにある。`plan` ツールはルータターンをまたぐ multi-source 合成のためのチャットセッション機構であり、Phase は定義済みの input/output コントラクトを持つため Phase 内に対応物はない。

- **`shell` は Phase-only。** `shell` をチャットルーターに直接公開すると、スキーマ境界なしの自由形式の会話コンテキストで LLM が任意コマンドを実行できてしまう。Phase モデルはこれを制約する — `shell` は Skill ごとにオプトイン、`allowed_ops` でゲート、Phase の input schema がコマンドに到達するデータを絞り込む。

- **`lint` は Phase-only。** Lint は Phase 中に LLM の Skill オーサリング出力を検証する。Skill artifact を生成しないチャットルーターには用途がない。

- **`ask_user` は Phase-only の明示的 op。** router LLM はプレーンテキスト応答を emit することでユーザーに質問する — `RouterLoop` はそのテキストで終了する。Phase LLM は mid-phase で終了できないため、`control_ir` 内の `ask_user` を使用して一時停止し OS に質問を表面化する必要がある。

### Type C — Convention drift

原則なしに時間とともに生まれた非対称性であり、役割ベースの強い根拠がないもの。

- **メモリ I/O が router-only。** `list_memory`、`read_memory_body`、`remember_shared`、`remember_agent`、`forget_memory` がチャットルーターで利用可能。Phase は Phase 開始時に context_builder 経由で注入されたメモリを受け取る（読み取り専用スナップショット）が、mid-phase でメモリの照会・更新はできない。Phase がメモリを書き込めない原則的なアーキテクチャ上の理由はない — このギャップはメモリツールが直接ユーザーインタラクション用にルーターへ追加され、対応する Phase 機能が設計されなかったことで生じた。

- **カタログ閲覧が router-only。** `list_skills`、`describe_skill`、`list_agents`、`describe_agent` がチャットルーターで利用可能。Skill やエージェントのカタログデータが必要な Phase（例: `eval_builder` や `skill_improver`）は ContextFrame データ（`op_catalog`）として注入されたカタログを受け取るが、mid-phase カタログクエリは発行できない。このギャップはカタログ閲覧が主にルーターの「どの Skill を呼び出すか」決定に有用だったため生じた。

- **MCP 検索が router-only。** `list_mcp_servers`、`list_mcp_tools` がチャットルーターで利用可能。`mcp` op を使用する Phase は `control_ir` にサーバー名とツール名を静的に宣言しなければならない。このギャップは MCP 閲覧がルーターのインタラクティブな「MCP で何ができるか」ユースケースのために追加され、Phase 側 `mcp` op への対応する検索機構なしに実装されたことで生じた。

これらはギャップであり、失敗ではない。閉じるかどうかはセクション 6 の doctrine 問題が決定する。

### Type D — LLM 呼び出し前の決定論的ステップ

Preprocessor および postprocessor ステップは LLM invocation ではないが、「Skill オーサーが使えるもの」として機能比較の議論に登場する。区別が重要:

- `python` preprocessor ステップはサンドボックス化された Python コードを実行する — LLM 呼び出しなし
- `run_skill` preprocessor ステップは sub-skill を呼び出し、その Phase が Phase-style で LLM を呼び出す — ただし preprocessor ディスパッチ自体は同期的であり OS 制御であり、同一ターンでの LLM 呼び出しではない
- `validate` ステップは JSON Schema チェックを実行する — LLM 呼び出しなし

Preprocessor と postprocessor ステップは LLM 呼び出しの前後に Phase が計算できることを拡張するが、第3の invocation kind を構成しない。

---

## 5. なぜ乖離が生まれたか — 歴史的パターン

チャットルーターは新機能が追加されるたびにツール追加で機能を蓄積してきた — メモリ I/O、カタログ閲覧、web ops、plan モード、Reyn ソースアクセス。各追加はそのコンテキストで自然だった（チャットユーザーがメモリについて直接質問したい、インタラクティブにカタログを閲覧したい、会話ターンでウェブを検索したい）。Phase Control IR op セットはより保守的に成長した（最大 23 router tools に対して 8 op kind）— Reyn の Phase モデルが制約された candidate set（P4）と Skill オーサーの意図を重視するためである。Phase は何を行うことが許可されているかを宣言し、それ以上ではない。結果として router はインタラクティブ探索機能を蓄積し、Phase surface はドメイン作業に焦点を絞ったまま残った。これは役割分離の理由が妥当な場合（Type B）は適切だが、そうでない場合（Type C）は convention drift である。

---

## 6. Doctrine オプション

問い: **Convention drift のギャップ（Type C）は閉じるべきか？** このセクションは3つのオプションを提示する。選択は別の決定事項であり、本ドキュメントはフレームワークを確立する。

### Option 1 — 完全な対称性

すべての機能を両 surface で適切な呼び出し形式で利用可能にする。Type B の例外（shell、lint、ask_user、plan、delegate_to_agent）は文書化された例外として保持する。

- **Pros:** クリーンな doctrine; 明示的な機能別選択以外の非対称性なし; 貢献者はシンプルなデフォルトルール（「理由がない限り両方に追加する」）を持てる
- **Cons:** 一部の機能は両側に自然にフィットしない（Phase 実行途中にピア agent へ委任することはオーケストレーションとドメイン作業を混同する）; surface area が増大; 新機能ごとに2-surface 実装が必要

### Option 2 — 役割ベースの非対称性（現状を追認）

現在の非対称性を doctrine として文書化する。Router はオーケストレーション、Phase はドメイン作業、機能は役割のどちらか一方にのみ属する。Type C ギャップはそのまま受け入れる。

- **Pros:** 変更最小; 既存の動作を明文化; 貢献者は明確なルール（「これはオーケストレーションかドメイン作業か？」）を持てる; 実装コストなし
- **Cons:** Type C ギャップを再検討せずにゴム印を押す; Phase からのメモリ書き込みは正当なニーズだがこのオプションでは未解決; より複雑な Skill がより豊富な Phase 側機能を必要とするにつれて doctrine が陳腐化する可能性

### Option 3 — ハイブリッド: Type C のみ閉じる

Type B には Option 2 の役割分離を採用しつつ、3つの Type C convention drift ギャップを明示的に閉じる。

- **Phase からのメモリ書き込み:** 新しい `memory` op kind（または `update_memory` のような stdlib skill）により、Phase がチャット層を経由せずに永続的な事実を書き込める
- **Phase からのカタログ閲覧:** stdlib skill（例: `recall_skill_catalog`）により Phase が `run_skill` 経由でライブカタログを mid-phase クエリできる（OS にカタログ知識を埋め込まずに）
- **Phase からの MCP 検索:** `mcp` op を `action=list_servers` および `action=list_tools` バリアントで拡張し、Phase が実行時に利用可能な MCP 機能を探索できる

- **Pros:** 原則的 — 役割分離が実在する場合は役割ベース（Type B）、ギャップが意図しないものだった場合は対称（Type C）; doctrine に技術的負債が蓄積されない; 新機能が最初から両 surface を念頭に設計される
- **Cons:** 中程度の実装コスト（3つの新機能）; 順序が重要（phase op 拡張より前に stdlib skills）; 次の追加バッチで drift を再生成しないよう規律が必要

---

## 7. 既存の原則との接続

**P3（OS が実行を制御する）** — 両 invocation kind は OS が仲介する。router LLM はツールを呼び出し、OS がディスパッチする。Phase LLM は `control_ir` を emit し、OS がそれらの op をディスパッチする。どちらの surface も LLM が直接実行することは許可しない。Doctrine の問いは OS が各 kind にどの機能を公開するかについてであり、誰が実行を制御するかではない。

**P4（LLM は制約された決定エンジン）** — 両 invocation kind は厳選された candidate set を提示する。router LLM は `build_tools()` で組み立てられた固定ツールリストを見る。Phase LLM は Phase の `allowed_ops` から構築された `available_control_ops` を見る。Doctrine は各 kind がどの候補を見るかについてであり、P4 は両側に等しく適用される。

**P7（OS は Skill に依存しない）** — どちらの surface も Skill 固有の知識を埋め込むべきではない。stdlib skills を通じて Type C ギャップを閉じる（Option 3 パス）ことで P7 を保全する — OS は汎用の `memory` op や `run_skill` 機構を公開し、Skill オーサーがそれを使用するかどうかを決める。Skill 固有のメモリキーやカタログパスを OS 層に埋め込むことは P7 違反となる。

---

## 8. 関連ドキュメント

- [principles.md](principles.md) — P3、P4、P7
- [architecture.md](architecture.md) — コンポーネント全体の階層化とランタイムループ
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — Phase・Skill・OS 間の責任境界
- [care-boundary.md](care-boundary.md) — Reyn が担うこと・担わないこと; downstream tooling セクションは上記マトリックスを補完する
- [preprocessor.md](preprocessor.md) — LLM 呼び出し前の決定論的ステップ（= 第3の invocation kind ではない理由）
- [postprocessor.md](postprocessor.md) — LLM 呼び出し後の決定論的ステップ（同理由）
- [../reference/runtime/control-ir.md](../reference/runtime/control-ir.md) — Phase 側 op の語彙とセマンティクス
- [../reference/cli/chat.md](../reference/cli/chat.md) — チャットで使用可能なスラッシュコマンド（router tools と混同されることがあるが別物）
- [../reference/cli/mcp.md](../reference/cli/mcp.md) — MCP サーバー側（Reyn-as-MCP-server は外部クライアントが Reyn を呼び出す第3の surface を公開するが、Reyn 内部の LLM invocation kind ではないため本ドキュメントでは扱わない）

---

## 9. 実装: 統合 tool registry（M1 着地済み、M2 待ち）

本ドキュメントで説明した二重実装アーキテクチャ（`router_tools.py` / `OP_KIND_MODEL_MAP` の 2 つのカタログ）は歴史的ベースラインである。
ADR-0026（ステータス: Proposed）は、1 つの `ToolDefinition` に 2 つの render メソッドを持たせることで構造的なドリフトを解消する。

**M1 ステータス（着地済み）:** インフラモジュール `src/reyn/tools/` が存在する:

- `ToolDefinition`, `ToolGates`, `ToolContext`, `ToolHandler`, `ToolResult` — `src/reyn/tools/types.py`
- `ToolRegistry` — `src/reyn/tools/registry.py`
- `invoke_tool`, `ToolNotFound`, `ToolGateRefused` — `src/reyn/tools/dispatch.py`

現時点ではどのケーパビリティも移行されていない。`build_tools()` と `OP_KIND_MODEL_MAP` が引き続き有効なディスパッチパスである。M2 POC で `web_search` を最初のケーパビリティとして移行し、M3 で残り 12 件を順次移行、M4 でレガシー構造を削除する。

**参照:** [../deep-dives/decisions/0026-unified-tool-registry.md](../deep-dives/decisions/0026-unified-tool-registry.md)
