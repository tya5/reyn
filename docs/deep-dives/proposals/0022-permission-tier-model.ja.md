# FP-0022: パーミッション Tier モデル正式化 — 2 軸フレームワークの明文化

**Status**: **done** — LANDED 2026-05-14 (commit `61dc193`): `require_web_fetch()` + handler-level 4-layer 承認 + `web_search` config-deny path + tier model docs (en+ja)
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

パーミッションシステムには **利用宣言**（スキルが意図を表明する）と **許諾**（オペレーター/ユーザーがアクセスを認可する）という 2 軸があるが、どちらも明文化されておらず、op ごとに適用される層が非対称になっている。本提案は Android の Normal/Dangerous permission 区分と同構造の 4 Tier モデルを正式化し、具体的な非対称さを 2 点修正する。① `web_fetch` が catalog-level の config ゲートのみを使い 4 層インタラクティブ承認スタックを迂回している問題。② `web_search` に config による制限経路がない問題。

---

## Motivation

### 2 軸モデル（現在は暗黙）

**軸 1 — 利用宣言**（`skill.md` frontmatter の `permissions:` ブロック）:  
スキル作者が何を使う意図があるかを宣言する。宣言されていない op は即 `PermissionError`——スキルにそのアクションを行う意図がない（Android でマニフェスト宣言なしに API を呼ぶと SecurityException になるのと同じ）。

**軸 2 — 許諾**（オペレーター/ユーザーがアクセスを認可）:  
`PermissionResolver._approve()` の 4 解決層：

| 層 | ソース | 誰が | 永続性 |
|---|---|---|---|
| 1 | `reyn.yaml` の `permissions.<key>: allow/deny` | オペレーター | 静的ファイル |
| 2 | `.reyn/approvals.yaml` | ユーザー（ALWAYS/NEVER） | 跨セッション |
| 3 | `self._session[key]` インメモリ | ユーザー（YES/NO） | 1 セッション限り |
| 4 | インタラクティブプロンプト | ユーザーがリアルタイム回答 | → 層 2 or 3 へ |

### 現状の非対称さ

| Op | 利用宣言 | 許諾層 | あるべき姿 |
|---|---|---|---|
| `shell` | `decl.shell` 必須 | 4 層 | ✓ Tier 3 |
| `mcp` | `decl.mcp` 必須 | 4 層 | ✓ Tier 2 |
| `file`（zone 外） | `decl.file_*` 必須 | 4 層 | ✓ Tier 3 |
| `web_fetch` | なし | config のみ（層 1） | ✗ → Tier 1（4 層） |
| `web_search` | なし | 0 層（常に通過） | ✗ → Tier 1（config deny） |
| `run_skill`、`ask_user` | なし | 0 層 | ✓ Tier 0 |

`web_fetch` は `web.fetch: allow` を config に設定しない限り静かに使用不能になる。ユーザーにはプロンプトが表示されず、LLM はツールが存在することすら知らない。ユーザーが「何か調べて」と頼んでもエージェントが理由なく断るという UX 上の問題が発生する。

### Android との対比

Android は Normal permission（自動許可、マニフェスト宣言のみ）と Dangerous permission（実行時にユーザー承認が必要）を区別する。Reyn の Tier モデルはこれと直接対応する：

- **Tier 0** = マニフェスト記載不要・実行時ゲートなし（常に有効な組み込み機能）
- **Tier 1** = Normal permission: 宣言不要、デフォルト許可、ただし config `deny` で制限可能
- **Tier 2–3** = Dangerous permission: 明示的な宣言必須＋ユーザー承認必須

---

## Proposed implementation

### Tier モデル（正式定義）

| Tier | 代表 Op | 利用宣言 | デフォルト | config 制限 |
|---|---|---|---|---|
| 0 | `run_skill`、`ask_user` | 不要 | 無条件通過 | 不可（アーキテクチャが壊れる） |
| 1 | `web_search`、`web_fetch` | 不要 | 許諾 | ✓ `deny` で制限可能 |
| 2 | `mcp` | 必要 | 要承認（4 層） | ✓ `allow` で事前許可 |
| 3 | `shell`、`file`（zone 外） | 必要 | 要承認（4 層） | ✓ `allow` で事前許可 |

Tier 0 は「デフォルト許可」ではなく「**無条件通過**」——これらの op をブロックする config キーは存在しない（存在してはいけない）。

### 変更 1 — `web_fetch`: catalog ゲート → handler-level `_approve()`

**`src/reyn/permissions/permissions.py`** に追加：

```python
async def require_web_fetch(self, url: str, bus: InterventionBus) -> None:
    """Tier 1 gate for web_fetch — 利用宣言不要、4 層許諾フル通過。"""
    if not await self._approve("web.fetch", f"web fetch: {url}", bus):
        raise PermissionError("web fetch denied")
```

**`src/reyn/op_runtime/web.py`** — `handle_web_fetch()` 先頭に追加：

```python
if ctx.permission_resolver is not None:
    if ctx.intervention_bus is None:
        raise RuntimeError("web_fetch op requires intervention_bus on OpContext")
    await ctx.permission_resolver.require_web_fetch(op.url, ctx.intervention_bus)
```

**`src/reyn/chat/services/router_host_adapter.py`**:
- `get_web_fetch_allowed()` と呼び出し箇所を削除
- router catalog に `web_fetch` を常時含める（条件分岐を削除）

**`src/reyn/chat/router_tools.py`**:
- `web_fetch_allowed` パラメータと条件分岐を削除

**変更後のデフォルト動作**:
- config 未設定 → 初回実行時にインタラクティブプロンプト（YES/NO/ALWAYS/NEVER）
- ALWAYS → `.reyn/approvals.yaml` に永続化、次回から確認なし
- `web.fetch: allow` → 事前許可、確認なし（既存の動作を保持）
- `web.fetch: deny` → 即 `PermissionError`

### 変更 2 — `web_search`: config `deny` 経路を追加

**`src/reyn/op_runtime/web.py`** — `handle_web_search()` 先頭に追加：

```python
if ctx.permission_resolver is not None and ctx.permission_resolver._is_config_denied("web.search"):
    raise PermissionError("web search denied by config (web.search: deny)")
```

デフォルト動作は変わらず（常に通過）。`reyn.yaml` に `web.search: deny` を設定した場合のみブロック。web search は読み取り専用・副作用なしのため、インタラクティブ承認は不要。

### 変更 3 — ドキュメント更新

**`docs/concepts/permission-model.md`**:
- 「Tier モデル」セクションを追加（上記のテーブル）
- 2 軸フレームワーク（利用宣言 vs 許諾）の説明を追加
- `web.fetch` と `web.search` の config キーを明文化

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/permissions/permissions.py` | `require_web_fetch()` 追加 |
| `src/reyn/op_runtime/web.py` | `handle_web_fetch()` に `require_web_fetch()` 呼び出し追加；`handle_web_search()` に `_is_config_denied()` 追加 |
| `src/reyn/chat/services/router_host_adapter.py` | `get_web_fetch_allowed()` 削除；`web_fetch` を常時 include |
| `src/reyn/chat/router_tools.py` | `web_fetch_allowed` 条件分岐を削除 |
| `docs/concepts/permission-model.md` | Tier モデル + 2 軸の説明を追加 |

---

## Dependencies

なし。`_approve()` と `_is_config_denied()` はすでに存在する。`OpContext` には `permission_resolver` と `intervention_bus` フィールドがすでにある（mcp handler が先例）。

既存の `web.fetch: allow` config エントリは引き続き動作する——`_is_config_approved()` が層 1 でこれを処理し、インタラクティブプロンプトをスキップする。

---

## Cost estimate

| タスク | コスト |
|---|---|
| `require_web_fetch()` 追加 + handler 呼び出し | SMALL |
| router catalog ゲート削除 | SMALL |
| `web_search` deny check 追加 | SMALL |
| `docs/concepts/permission-model.md` 更新 | SMALL |
| **合計** | **SMALL** |

すべての変更は小さな独立した呼び出し箇所での加算または削除。プロトコル変更なし。`.reyn/approvals.yaml` の既存承認はそのまま機能する。

---

## Related

- `src/reyn/chat/services/router_host_adapter.py` — `get_web_fetch_allowed()` の削除対象
- `src/reyn/permissions/permissions.py` — 再利用する `_approve()`、`_is_config_denied()`
- `docs/concepts/permission-model.md` — 拡張対象のドキュメント
- FP-0021 (`0021-event-log-audit-completeness.ja.md`) — 同セッションで起票
- Android Normal/Dangerous permission モデル — 設計の先例
