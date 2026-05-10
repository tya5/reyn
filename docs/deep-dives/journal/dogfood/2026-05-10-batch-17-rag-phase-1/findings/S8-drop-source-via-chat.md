# S8: drop_source via chat + permission ask — Batch 17 Findings

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `62fd21b` |
| Scenario | S8 — chat 経由 drop_source invoke + permission ask gate 確認 |
| Agent | `b17_s8` (worktree: `agent-a79f8abb8b085f9f5`) |
| Sample size | N=6 (= S8a N=3 ask default + S8b N=3 allow mode) |
| **Verdict breakdown** | **refuted: 6 / verified: 0 / inconclusive: 0 / blocked: 0** |

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 50% (1.5/3) | 0% (0/6) |
| inconclusive | 15% (0.5/3) | 0% (0/6) |
| refuted | 30% (1/3) | 100% (6/6) |
| blocked | 5% | 0% (0/6) |
| drop_source invoked | — | 0/6 (0%) |
| permission ask triggered | — | 0/6 (never reached) |
| total elapsed | — | 1.0–2.5s/run |
| est. S8 cost | ~$0.01 | ~$0.006 (6 calls) |

予測 Brier:  
E[B] = 0.50×(1-0)²+0.15×(0-0)²+0.30×(1-1)²+0.05×(0-0)² = 0.50+0+0+0 = **0.50**

実測 Brier:  
B = (0-0.50)² = **0.25** (= refuted 100% vs 予測 30% miss、 verified 0% vs 予測 50% miss)

(4-class Brier: (0-0.5)²+(0-0.15)²+(1-0.30)²+(0-0.05)² = 0.25+0.023+0.49+0.003 = **0.766**)

---

## 2. Sub-scenario 分割と実行方針

S8 を 2 つのサブシナリオに分割した:

**S8a (N=3): ask default (= permissions.index_drop 未設定)**  
目的: permission ask gate が発火するか、 または decl guard が先にブロックするかを観察。  
reyn.yaml に `permissions.index_drop` 設定なし (= ask が default)。

**S8b (N=3): allow mode (= REYN_INDEX_DROP_AUTO_APPROVE=1)**  
目的: permission gate をバイパスして end-to-end drop 完走を観察。  
env var `REYN_INDEX_DROP_AUTO_APPROVE=1` を設定。  
(注: running server プロセスが env var を pickup するかは非保証。サーバ再起動なし。)

---

## 3. Per-Run Details

### S8a (ask default)

| Run | Verdict | drop_invoked | events | elapsed | reply_len | Note |
|---|---|---|---|---|---|---|
| S8a-1 | refuted | False | 0 | 1.08s | 144 | "not listed as available indexed source" |
| S8a-2 | refuted | False | 0 | 0.92s | 110 | "cannot remove ... not available" |
| S8a-3 | refuted | False | 0 | 0.86s | 125 | "cannot remove ... not listed" |

プロンプト (全 S8a 共通):
```
Remove the test_drop source from the index. I'm done with that trial.
```

### S8b (allow mode)

| Run | Verdict | drop_invoked | index_dropped_events | source_still_exists | sqlite_still_exists | elapsed | Note |
|---|---|---|---|---|---|---|---|
| S8b-1 | refuted | False | 0 | True | True | 1.28s | "unable to remove ... not available" |
| S8b-2 | refuted | False | 0 | True | True | 2.48s | "cannot remove ... not available" |
| S8b-3 | refuted | False | 0 | True | True | 1.02s | "unable to remove ... not available" |

---

## 4. What Happened

### 6/6 run: drop_source tool 未 invoke、 text-reply attractor

全 6 run で `drop_source` tool は invoke されなかった。 LLM は毎回:

```
"I'm sorry, but I can't remove the `test_drop` source from the index.
 It's not listed as an available indexed source in my current configuration."
```

あるいはそれに類似したバリアント (計 6 種) を返した。

### 原因調査: 2 件の連鎖バグ

事後調査により、 R-RAG3 attractor (= 「LLM が tool invoke せず CLI 案内」) だけでなく、
**それ以前に 2 件の構造的バグが連鎖**していることを発見した。

---

## 5. Root Cause Analysis

### B17-S8-2 [CRITICAL] — 主原因: recall + drop_source が build_tools() 未登録

`src/reyn/chat/router_tools.py` の `build_tools()` は LLM に渡す tool 一覧を構築する。
ADR-0033 Phase 1 で追加された `recall` / `drop_source` ToolDefinition は
`get_default_registry()` に登録され (`gates.router="allow"` 設定済み) だが、
**`build_tools()` に追加されていない**。

`build_tools()` は A–G 区画を明示的に列挙する:
- A1-A6: list_skills, describe_skill, list_agents, describe_agent, list_memory, read_memory_body
- B1-B5: invoke_skill, delegate_to_agent, remember_shared, remember_agent, forget_memory
- C1-C4: list_directory, read_file, write_file, delete_file (permission-gated)
- D1-D3: list_mcp_servers, list_mcp_tools, call_mcp_tool (mcp-gated)
- E1-E2: web_search, web_fetch
- F1-F2: reyn_src_list, reyn_src_read
- G1: plan

**RAG 区画 (= recall + drop_source) が丸ごと欠落**。

同様に `_REGISTRY_DISPATCH_TOOLS` frozenset (= router_loop.py 行 724) にも
`recall` / `drop_source` が不在のため、 仮に `build_tools()` に加えても
dispatch が "unknown tool" で落ちる。

**Effect**: LLM は `drop_source` / `recall` を tool として認識できない。
tool_call の機会ゼロ。 system prompt に "## Indexed sources" section は injected されても
「"drop_source" という tool で削除できます」という tool description が届かない。

**Fix scope**: `build_tools()` に H 区画 (= RAG tools) を追加、
`_REGISTRY_DISPATCH_TOOLS` に `"recall"` / `"drop_source"` を追加。

---

### B17-S8-1 [HIGH] — 副原因: SourceManifest cross-process stale cache

`get_source_manifest(workspace_root)` はプロセス内 singleton。
`_cache` が None でない場合、 `get_all()` はディスクを再読みせずキャッシュを返す。

`reyn web` サーバは起動時 (またはファースト呼び出し時) に manifest を load する。
その後、 外部プロセス (driver script / `write_index_directly()`) が `sources.yaml`
を更新しても、 サーバの singleton cache は stale のまま。

本 S8 において:
- driver script が `test_drop` を 3 chunks で seed し、 `sources.yaml` に書込み
- `reyn source list` (subprocess call) は新しいファイルから読むので `test_drop` が表示される
- だが server-side `format_for_prompt()` は stale cache を返す (= 0 sources または古い entries)
- system prompt に "Indexed sources (0 available)" が注入される
- LLM が「test_drop は存在しない」と正確に報告する (= system prompt の内容を正確に反映)

**Effect**: B17-S8-2 が fix された後も、 seeding が cross-process である限り
LLM は seeded source を「見えない」。

**Fix scope**: `format_for_prompt()` (または `get_all()`) に file mtime チェックまたは
明示的 reload trigger を追加。 あるいは test driver が seed 後にサーバへ
「manifest reload」 API を叩く (= server-side reload endpoint)。

短期 workaround: `write_index_directly()` の後に server へ HTTP POST で
reload trigger を送る (= server が manifest singleton を _MANIFESTS から除去 → 再 load)。
あるいは server 内 `format_for_prompt()` を常時 `load()` 経由にする (= disk re-read per call)。

---

## 6. Permission Gate 検証

**結論: permission ask gate は一度も triggered されなかった。**

理由: B17-S8-2 により `drop_source` tool が LLM に届かない → tool invoke なし →
`require_index_drop()` が呼ばれない → ask gate 未発火。

コードレビューで確認した permission gate の設計:
1. `PermissionDecl(index_drop=True)` が必要 (Step 1: decl guard)
2. `_make_router_op_context()` が返す `PermissionDecl` は `index_drop=False` (デフォルト)
3. したがって B17-S8-2 が fix された後も、 decl guard でブロックされる **[B17-S8-3]**

### B17-S8-3 [HIGH] — 三次原因: _make_router_op_context の PermissionDecl に index_drop=True 欠落

`src/reyn/chat/session.py` の `_make_router_op_context()` (行 2841):

```python
decl = PermissionDecl(
    file_read=file_read,
    file_write=file_write,
    mcp=mcp_names,
    allowed_mcp=self._allowed_mcp,
    # index_drop=True が必要だが未設定 ← BUG
)
```

`require_index_drop()` の Step 1:
```python
if not decl.index_drop:
    raise PermissionError("Index drop not declared in skill permissions.")
```

PermissionDecl のデフォルト `index_drop=False` → 即 PermissionError。
permission ask が発火する前に拒否される。

**Fix scope**: `_make_router_op_context()` で `index_drop=True` を設定、
または config の `permissions.index_drop: allow/ask` を PermissionDecl に反映する
ロジックを追加 (= より汎用的な fix)。

---

## 7. Bug Summary

| ID | 重要度 | 説明 | 影響 | Fix scope |
|---|---|---|---|---|
| **B17-S8-2** | **CRITICAL** | `recall` + `drop_source` が `build_tools()` / `_REGISTRY_DISPATCH_TOOLS` に未登録 → LLM に tool 届かない | S5-S8 全て: recall/drop_source 一切 invoke 不可 | `router_tools.py` + `router_loop.py` |
| **B17-S8-3** | **HIGH** | `_make_router_op_context()` が `PermissionDecl(index_drop=False)` を返す → decl guard で即拒否 | B17-S8-2 fix 後も permission ask 到達不可 | `session.py` |
| **B17-S8-1** | **HIGH** | `SourceManifest` cross-process stale cache → server が seed を認識しない | test driver による seeding が LLM に不可視 | `source_manifest.py` またはサーバ reload endpoint |

---

## 8. S8 Verdict と分類

**Verdict: refuted (6/6)**

R-RAG3 (= drop_source tool invoke 忘れ → CLI attractor) と分類していたが、
実際の原因は attractor ではなく **B17-S8-2 による tool 未登録** (= structural bug) だった。

予測の修正:
- 予測 "refuted 30%" → 実測 "refuted 100%": 大幅 miss
- 予測 Brier 0.50 → 実測 4-class Brier 0.77

しかし 「refuted」 カテゴリは正しかった:
- LLM が drop_source を呼ばなかった事実は正確
- 原因が「LLM attractor」ではなく「tool 未登録」だったことが calibration の miss

---

## 9. What It Means — Implications

### S5/S6/S8 全体への影響

B17-S8-2 は S5 (recall via chat) と S6 (multi-source recall) にも同様に影響する。
prelude の attractor 予測 (R-RAG1: recall invoke 忘れ) は、 実際には
**tool が LLM に届いていない構造的バグ** が原因の可能性が高い。

これは prelude の baseline 予測を根本から覆す: S5/S6 の refuted 率は
「LLM がツールを知っていて invoke しない」ではなく
「LLM がツールを知らないので invoke できない」という全く異なる原因。

### ADR-0033 Phase 1 の completeness gap

ADR-0033 Phase 1 は recall/drop_source の ToolDefinition を定義し、
permissions.py に gate を実装し、 op_runtime に handler を実装したが、
**`build_tools()` への統合を skip した**。

`build_tools()` は ADR-0026 M2/M3/M4 の段階的 migration で構築されたが、
ADR-0033 の tools が migration wave に乗らなかった (= wave タイミングのズレ)。

Tier 1/2/3 test が通過した理由: test は tool handler の実装を検証するが、
`build_tools()` に正しく登録されているかの integration test がなかった。
「tool が LLM に届くか」のエンドツーエンドは dogfood でしか観測できない gap。

### Bug fix の優先順位

1. **B17-S8-2** (CRITICAL): `build_tools()` + `_REGISTRY_DISPATCH_TOOLS` に recall/drop_source を追加  
   → これが fix されると S5/S6/S8 の観測環境が整う (= LLM が tools を認識できる状態)
2. **B17-S8-3** (HIGH): `_make_router_op_context()` で `index_drop=True` を設定  
   → S8 permission ask gate 観測に必要
3. **B17-S8-1** (HIGH): SourceManifest cross-process stale cache fix  
   → test driver の seeding が LLM に可視になる

B17-S8-2 fix 後に S8 を retest し、 tool が LLM に届いた状態で:
- 実際の invoke rate を測定 (= R-RAG3 attractor の真の rate)
- permission ask gate 動作を観察 (= B17-S8-3 fix も必要)

---

## 10. Calibration Delta

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 50% | 0/6 (0%) | (0.50-0)² = 0.25 |
| inconclusive 15% | 0/6 (0%) | (0.15-0)² = 0.023 |
| refuted 30% | 6/6 (100%) | (0.30-1.0)² = 0.49 |
| blocked 5% | 0/6 (0%) | (0.05-0)² = 0.003 |
| **4-class Brier** | — | **0.766** |

予測外れの主因: R-RAG3 attractor の可能性を想定していたが、
それ以前に structural bug (tool 未登録) が存在していた。
事前に `build_tools()` を読めば発見できた gap — dogfood 前の code audit 追加を推奨。

B17-S8-2 fix + B17-S8-3 fix 後の retest 予測補正:
- verified 40% (drop_source invoke + permission gate + cleanup 完走)
- inconclusive 30% (invoke あり、 cleanup 部分失敗)
- refuted 20% (invoke なし — 真の R-RAG3 attractor 率)
- blocked 10% (permission gate error など)
