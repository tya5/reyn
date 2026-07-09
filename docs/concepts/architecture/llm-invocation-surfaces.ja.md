---
type: concept
topic: architecture
audience: [human, agent]
---

# LLM invocation surfaces — router-style tool contract

> **ステータス: 一部陳腐化。** このページは元々2つの invocation kind を比較していました:
> チャットルーター(function-calling tools)と、削除済みワークフローエンジン内の
> phase executor(JSON `control`/`artifact`/`control_ir` 出力契約、後の
> engine 削除で削除済み)。phase-style surface とそれとの比較を前提にした節(機能比較
> マトリックス、4つの乖離タイプ、router/phase ギャップを閉じる doctrine オプション)は、
> 比較対象の片側が既に存在しないため削除しました — `OSRuntime` と
> `control`/`artifact`/`control_ir` エンベロープが現行ソースに存在しないことを直接
> grep で確認済みです。第4節(統合 `ToolRegistry` 実装ログ)は現行アーキテクチャを
> 記述しており正確ですが、`gates(phase=...)` への言及は今や vestigial です(消費する
> phase surface が無い)。

## 1. なぜこれが重要か

Reyn は `RouterLoop`(インタラクティブチャットセッション)経由のネイティブ function-calling tools で LLM を呼び出す。コンテキストごとにカタログを絞り込む `RouterLoopHost` facade を用いる。本ドキュメントはこの invocation surface とそのツール一覧を記述する。

---

## 2. Router invocation surface

### 2.1 Router-style（チャット）

**使用箇所:** `RouterLoop`（インタラクティブチャットセッション）。コンテキストごとにカタログを絞り込む `RouterLoopHost` facade を用いる。

**仕組み:** litellm 経由の `call_llm_tools` によるネイティブ LLM function calling。ツール定義は OpenAI `tools` 配列形式に従い、モデルはアシスタントメッセージ内の `tool_calls` で応答する。OS は各呼び出しをディスパッチし、`tool_result` を追記して、モデルが通常テキストを返すまで LLM を再呼び出しする。

**ツール surface:** `src/reyn/runtime/router_tools.py` の `build_tools()` がツールリストを組み立てる。実際の数はオペレーター設定に依存する。

- **常時存在（13 tools）:** `list_skills`, `describe_skill`, `list_agents`, `describe_agent`, `list_memory`, `read_memory_body`, `delegate_to_agent`, `remember_shared`, `remember_agent`, `forget_memory`, `web_search`, `reyn_src_list`, `reyn_src_read`
- **条件付き（+0〜+9 tools）:** `invoke_skill`（ワークフロー登録時）、`list_directory` + `read_file`（file read スコープ設定時）、`write_file` + `delete_file`（file write スコープ設定時）、`web_fetch`（オペレーターのオプトイン）、`list_mcp_servers` + `list_mcp_tools` + `call_mcp_tool`（MCP servers 設定時）
- **実測レンジ: 13–22 tools**（`router_tools.py` のコメントに記載の「11–18」は `web_search`、`reyn_src_list`、`reyn_src_read` 追加前の記述であり、現在は stale）

**役割:** オーケストレーション — 次のサブコンポーネント（ワークフロー、Agent、plan、メモリ操作、直接テキスト応答）を選択する。

---

## 3. 関連ドキュメント

- [../architecture/care-boundary.md](../architecture/care-boundary.md) — Reyn が担うこと・担わないこと
- [../../reference/runtime/control-ir.md](../../reference/runtime/control-ir.md) — OS がディスパッチする op の語彙
- [../../reference/cli/chat.md](../../reference/cli/chat.md) — チャットで使用可能なスラッシュコマンド（router tools と混同されることがあるが別物）
- [../../reference/cli/mcp.md](../../reference/cli/mcp.md) — MCP サーバー側（Reyn-as-MCP-server は外部クライアントが Reyn を呼び出す第3の surface を公開するが、Reyn 内部の LLM invocation surface ではないため本ドキュメントでは扱わない）

---

## 4. 実装: 統合 tool registry（ADR-0026 Accepted）

この ADR が解消した二重実装アーキテクチャ（`router_tools.py` / `OP_KIND_MODEL_MAP` の 2 つのカタログ、かつては phase 側 surface も存在した）は歴史的ベースラインである。
ADR-0026 は、1 つの `ToolDefinition` に 2 つの render メソッド(うち phase 側 render は上記ステータスの通り今や vestigial)を持たせることで構造的なドリフトを解消する。

**M1（着地済み — commit `edd4c1b`）:** インフラモジュール `src/reyn/tools/` が存在する:

- `ToolDefinition`, `ToolGates`, `ToolContext`, `ToolHandler`, `ToolResult` — `src/reyn/tools/types.py`
- `ToolRegistry` — `src/reyn/tools/registry.py`
- `invoke_tool`, `ToolNotFound`, `ToolGateRefused` — `src/reyn/tools/dispatch.py`

**M2 POC（着地済み — commit `367b41c`）:** `web_search` が統合 registry に移行された最初のケーパビリティである。`build_tools()` は `render_for_router()` 経由で registry から `web_search` を導出し、従来の `ToolSpec` リテラルとバイト同一の出力を生成する（LLMReplay フィクスチャは変更なし）。すべての M2 検証ゲートが通過: byte-identity GREEN、drift test GREEN、フルスイート 1500 passed / 2 xfailed、mkdocs strict エラーなし。

**M3 Wave 1（着地済み — commit `ba4c5fe`）:** 7 ケーパビリティを移行 — `web_fetch`、`shell`、`lint`、`ask_user`、`delegate_to_agent`、`plan`、`reyn_src_list`、`reyn_src_read`。`ToolDefinition` に `dispatch_kind` フィールドを追加。Tier 2 invariant +99。

**M3 Wave 2（着地済み — commit `66435d1`）:** 17 ケーパビリティを移行 — file ops × 4 / MCP ops × 3 / memory ops × 5 / catalog ops × 4 / `invoke_skill`。§4 で識別した Type C convention-drift の 3 つのギャップをすべて `gates(router=allow, phase=allow)` で宣言的にクローズ（memory write phase-side、catalog browse phase-side、MCP discover phase-side）。Tier 2 invariant +127。全移行を通じて LLMReplay fixtures を保持。`reyn web` A2A エンドポイントのサニティチェックにより実 LLM リグレッションなしを確認。

13 ケーパビリティクラスター（= 26 ToolDefinitions）すべてが unified ToolRegistry に登録済みである。§4 で識別した Type C convention-drift のギャップは `gates(router=allow, phase=allow)` で宣言的にクローズされている。Phase-side Control IR dispatch が registry を消費するように配線する作業は M4 cleanup の範囲である。

**M4 Phase 2（着地済み）:** ToolContext の型拡張 — `router_state` と `phase_state` が loose `Any` から型付き sub-object（`RouterCallerState` / `PhaseCallerState`）に変わり、ADR-0026 Open Question #3 を解決。全フィールドはデフォルト `None` で段階的移行に対応。Tier 2 invariant +7。

**M4 Phase 3 step 1（着地済み）:** ハンドラ活性化 + per-call schema enrichment hook。6 つの design-revisit `NotImplementedError` stub（catalog 4 件 + `delegate_to_agent` + `plan`）が型付き `RouterCallerState` の callable フィールド経由で delegate するよう活性化された。`RouterCallerState` に 4 つの新規 callable フィールド（`list_skills_fn` / `describe_skill_fn` / `list_agents_fn` / `describe_agent_fn`）を追加。`ToolDefinition` に optional `schema_enricher` hook を追加し、`render_for_router(state=...)` が per-session 動的データを inject するために起動する（正準用途: `invoke_skill.name` / `delegate_to_agent.to` enums）。`router_tools.py` 内の残り 2 件のインライン `ToolSpec` リテラル（= `invoke_skill` + `delegate_to_agent`）を新 hook 経由で registry consumption に移行、byte-identity を保持。mis-wiring 契約: dispatcher が必要な callable を populate しない場合、ハンドラは記述的メッセージで `RuntimeError` を raise する。Tier 2 invariant +29。1754 passed / 2 xfailed。

**M4 Phase 3 step 2（着地済み — commit `649a426`）:** `RouterLoop._invoke_router_tool` が活性化済 6 tools (catalog ×4 + `delegate_to_agent` + `plan`) を if/elif tree ではなく `invoke_tool(get_default_registry(), ...)` 経由で dispatch するように切替。`RouterLoop._build_router_caller_state` が bound callbacks つき `RouterCallerState` を構築。catalog list-handler の戻り値 shape を bare list に緩和（= LLMReplay byte-identity 保持）。`_invoke_router_tool` 内の A1–A4 / B2 / G レガシー分岐を削除。

**M4 Phase 4 step 1（着地済み）:** `_DISPATCH_KIND` sidecar dict / `_TOOL_SPECS_STATIC_ASYNC` を `router_tools.py` から削除。`get_dispatch_kind(name)` は registry の `ToolDefinition.dispatch_kind` を直接参照。registry が schema render と dispatch posture 分類の両方の canonical source になった。

**M4 Phase 3.5（着地済み — 5 commits `0093667` / `2b1fe8d` / `3378051` / `a58c685` / `7482b33`）:** router-side cluster activations 完了。 残り 18 tools (file ×4 / mcp ×3 / memory ×5 / web ×2 / reyn_src ×2 / `invoke_skill`) も全て `invoke_tool(get_default_registry(), ...)` 経由 dispatch するようになった。 migration audit で識別した per-tool 設計課題は 3 つの bridge pattern を `RouterCallerState` に追加して解決:

1. **`op_context_factory: Callable | None`** — RouterLoop が `host.make_router_op_context` を bind し、 file / mcp / web handlers が operator-declared PermissionDecl + Workspace を受信。 legacy router branch と同等。
2. **`host: Any`** — MCP handlers が session-level MCPClient cache を保持するための duck-typed RouterHostAdapter 参照。
3. **Per-tool callable bridges** (`run_skill_fn` / `list_memory_fn` / `read_memory_body_fn` / `remember_fn` / `forget_fn`) — RouterLoop の private helper に bind されており、 chain_id propagation (`invoke_skill`) と agent-aware memory paths (memory cluster) を保持。

`RouterLoop._invoke_router_tool` は registry dispatch top-branch + 将来 cluster 用の placeholder コメントだけの薄い実装に。 `_normalise_router_tool_result` が handler 戻り値 shape (= op_runtime synthesis 由来の dict envelope) を legacy router branch が emit していた bare-string / bare-list shape に正規化し、 LLMReplay byte-identity を 5 cluster migration を通じて end-to-end で保持。

**M4 Phase 4（着地済み）:** phase-side migration 完了で architectural goal 達成。

- **Phase 4 step 1 (commit `ebe5786`)** — `_DISPATCH_KIND` sidecar dict 撤去、 `get_dispatch_kind()` が registry の `ToolDefinition.dispatch_kind` を直接参照。
- **Phase 4 step 2** — coarse-name `FILE_OP` / `MCP_OP` / `RUN_SKILL_OP` ToolDefinitions を `gates(phase="allow")` で registry 登録。 phase Control IR `kind` 値は registry entry に 1:1 マッピング。 `ControlIRExecutor.execute()` が `invoke_tool(get_default_registry(), op.kind, ...)` 経由 dispatch、 catalog building (`_build_phase_tool_catalog`) は registry から schema を読む。
- **Phase 4 step 3** — `OP_KIND_MODEL_MAP` は coarse-kind reference (= linter `ALL_OP_KINDS`、 `OP_PURITY` coverage) として残存; dispatch time には参照されない。 `op_runtime/<kind>.py` handlers は registry handlers が委譲する shared implementation として残存。
- **`is_op_allowed` helper** — legacy coarse-name `allowed_ops` declarations が将来 fine-grained `op.kind` にマッチするための prefix-wildcard membership。 forward-looking: phase Control IR は今日も coarse kinds を emit。

**tool 追加コスト** (steady state): `src/reyn/tools/<name>.py` 1 file + `__init__.py` の register 呼出 1 行 = router-or-phase tool で **2 touch points**。 新規 phase-side coarse op kind は加えて `OP_KIND_MODEL_MAP` entry (linter / purity coverage) + `schemas/models.py` の Pydantic `IROp` model = **3 touch points** が phase-eligible 新 kind の予算。 これが今後の tool-scope 拡大が amortise する base line。

ADR-0026 は **Accepted**。

**参照:** [../../deep-dives/decisions/0026-unified-tool-registry.md](../../deep-dives/decisions/0026-unified-tool-registry.md)
