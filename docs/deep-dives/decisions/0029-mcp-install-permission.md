# ADR-0029: `mcp_install` permission — install-time gating として permission system に追加

**Status**: Superseded by [#571 collapse arc Phase 5 (PR #631)](https://github.com/tya5/reyn/pull/631) (2026-05-23). The bool axis `mcp_install` was removed; install gating now flows through `file.write` (= `.reyn/mcp.yaml`) + `http.get` (= registry host) + `secret.write` (= per-env-key) via the standard list-axis resolvers. See `docs/concepts/permission-model.md` → "Collapse arc" for the canonical decomposition.
**Track**: Architecture — permission system の install-time gating layer (= 旧 design、 historical record)

> Historical context preserved below. The bool-axis shape this ADR proposed is no longer the active mechanism.

---

## 1. Context

### MCP server installation の現状 (= dogfood Step 0 で確認)

Reyn の permission system は **runtime gating** (= `call_mcp_tool` 実行時の server / agent 単位 check) を備えるが、 **install-time gating layer がゼロ**。 `reyn.yaml` の `mcp.servers:` への server 追加は:

- `reyn config set` コマンド経由 → permission check なし
- `reyn.yaml` 直編集 → 当然 gate なし
- `reyn init` テンプレート生成 → permission check なし
- 将来の `reyn mcp install <server>` (= 提案、 `reyn-mcp-cli-shape.md` 参照) → 現状 install-time gate 不在

つまり「どの server を Reyn に追加してよいか」 を policy で gate する layer が存在しない。

### Enterprise / OSS positioning context

競合分析 (= `docs/deep-dives/research/competitive/openclaw.md`) で確認した: **OpenClaw の "ambient authority" model** (= LLM がいつでも any tool を invoke 可能、 enterprise compliance 弱) に対する Reyn の差別化軸は permission gating granularity。 既存の `permissions.mcp:` (= runtime gate) + `AgentProfile.allowed_mcp` (= per-agent allowlist) は強いが、 「**そもそも怪しい server を install させない**」 layer が欠けると:

- enterprise sysadmin が「どの MCP server を team 全員が使ってよいか」 を policy で表現できない
- private registry を建てて override しても、 user が `reyn.yaml` を直編集して任意 server を追加できる loophole が残る
- compliance 監査 (= 「installed servers の audit trail」) が弱い

→ install-time gating は **ADR-0027 AuditSeal compliance** + **ADR-0026 Unified Tool Registry の permission integration** と同じ系統の architectural decision。 OSS launch の競合差別化要素として明示すべき。

---

## 2. Decision

### `permissions.mcp_install` を新設

`reyn.yaml` (or scope tier の各 layer) で declare 可能な、 OS-level permission key として `mcp_install` を追加。

```yaml
# reyn.yaml (project scope)
permissions:
  mcp_install: deny           # team 全員が server を追加できない
  # or
  mcp_install: allow          # 自由に追加可
  # or
  mcp_install: ask            # 都度 interactive prompt (default)
```

scope tier (= Claude Code 流 3 tier) との interaction:

```yaml
# ~/.reyn/config.yaml (user scope)
permissions:
  mcp_install: allow          # 個人開発機では自由

# <project>/reyn.yaml (project scope、 VCS commit)
permissions:
  mcp_install: deny           # team 共有 project では disable

# <project>/reyn.local.yaml (local scope、 gitignored)
permissions:
  mcp_install: ask            # 個人 dev override
```

`PermissionResolver._is_config_approved()` の汎用 key-dot 構造で透過的に動く (= 既存 implementation 拡張不要、 ただし辞書 key 追加のみ)。

### `PermissionDecl` への field 追加

```python
# src/reyn/permissions/permissions.py
@dataclass(frozen=True)
class PermissionDecl:
    # ... existing fields ...
    mcp_install: bool = False     # default deny、 explicit declaration が必要
```

skill / agent 側で `mcp_install: true` を declare した場合のみ、 そこから派生する Control IR `{kind: mcp_install}` op が permission gate を通過可能。

### `require_mcp_install()` resolver method 追加

既存 `require_mcp()` (= line 607-623 of `permissions.py`) と同じ pattern:

```python
async def require_mcp_install(self, decl: PermissionDecl, server_id: str) -> None:
    # 1. decl.mcp_install が False なら即拒否 (= skill / agent が install を declare してない)
    if not decl.mcp_install:
        raise PermissionError(...)
    # 2. config (各 scope tier) で deny されていないか
    if not self._is_config_approved("mcp_install"):
        raise PermissionError(...)
    # 3. interactive prompt (= "ask" default)
    await self._prompt_or_use_saved(f"mcp_install:{server_id}", ...)
```

approval key は `mcp_install:<server_id>` (= per-server granularity)、 `.reyn/approvals.yaml` に persist。

### `mcp_install` IR op との integration

`reyn-mcp-cli-shape.md` の `mcp_install` IR op が dispatch される時:

```
mcp_install IR op handler
  ├── 1. registry fetch (= server.json 取得)
  ├── 2. require_mcp_install(decl, server_id)   ← 新設の gate
  ├── 3. runtimeHint check (= npx / uvx 等)
  ├── 4. credentials 投入 flow (= --env / interactive prompt / dotenv save)
  ├── 5. reyn.yaml の mcp.servers.<name> 追記 (= scope tier 反映)
  └── 6. event: mcp_server_installed emit (= P6 audit truth、 AuditContext と連動)
```

### Private registry との combination

ADR の重要 point: `permissions.mcp_install: allow` + private registry override (= `mcp.registries:` priority list で private registry を先頭) の組み合わせで **「承認済み server registry の中からのみ install 可能」** policy が実現:

```yaml
# enterprise reyn.yaml (project scope)
mcp:
  registries:
    - https://mcp-registry.internal.acme.com/    # private registry (= 承認済み servers のみ)
    - https://registry.modelcontextprotocol.io/   # public fallback (= 信頼度低、 後ろ)
permissions:
  mcp_install: allow                              # team 全員が install 可能
  # ただし install 候補は private registry に登録された servers のみ事実上限定
```

`require_mcp_install()` は server_id が registry resolve 経由で来るため、 private registry にある server なら通り、 public のみにある server は registry fetch path で先に弾かれる (= layered defense)。

---

## 3. Consequences

### ✓ 得られるもの

- **enterprise differentiation as architecture**: OpenClaw ambient authority に対する明確な構造的差別化、 「audit + policy as code」 の差別化を architectural decision として確立
- **既存 permission system の自然な拡張**: 新 abstraction なし、 `PermissionDecl` field 1 個 + `require_*` method 1 個追加のみ。 P7 (= OS skill-agnostic) に違反しない (= `mcp_install` は OS-level concept、 skill-specific ではない)
- **scope tier との直交性**: `~/.reyn/config.yaml` (= 個人開発自由) / `<project>/reyn.yaml` (= team 共有 project で deny) / `<project>/reyn.local.yaml` (= override) を user が natural に使い分け可能
- **registry override との multiplicative defense**: private registry + `mcp_install: allow` の combination で「approved servers only」 policy
- **audit trail 統一**: `mcp_server_installed` event が ADR-0027 AuditSeal の入力ソースに将来統合可能 (= compliance reportで「いつ誰が何を install したか」 trace 可能)

### △ トレードオフ

- **OSS first-touch friction**: default `ask` でも初回 install で prompt が出る、 light user が confused になる risk (= mitigation: prompt message に「これは何を聞いてるか」 を明示、 docs Quick start で「`reyn mcp install` 初回は permission prompt 出ます」 と前置)
- **per-server granularity の複雑度**: `mcp_install:<server_id>` approval key が persist、 一度承認した server を後から取り消す UX が必要 (= `reyn permissions clear mcp_install:github` 等の補助 subcommand 検討)
- **`ask` default に対する CI 摩擦**: CI で `reyn mcp install` を回す ops が prompt で hang、 `--non-interactive` flag + config で deny 明示 or `REYN_MCP_INSTALL_AUTO_APPROVE=1` 環境変数 で skip 可能化が必要

### 不採用案

- **install-time gate を file.write permission で代替**: 「`reyn.yaml` への write を gate すれば足りる」 という案だが、 (a) MCP-specific な意図 (= 「server install 行為」) を file write 一般に薄める、 (b) registry resolve / runtimeHint check / credentials 投入 flow が gate を通った後に走る順序が file.write では表現できない (= write が先、 install logic が後)、 で却下
- **single binary `permissions.mcp_install: bool` (= scope tier なし)**: 個人 dev / team / enterprise の use case 差をカバーしきれず、 `permissions.mcp_install: deny` を project scope に強制する手が消えるため却下
- **per-server permission のみ (= `permissions.mcp_install.<server>: allow`)**: granular すぎて user が server 名を予め知っている前提、 「install 全般を deny する」 broad policy が表現できないため却下。 ただし default (= `mcp_install` global) + per-server override (= `mcp_install.<server>: allow|deny`) の hybrid は将来 phase 2 で検討余地

---

## 4. 実装コスト見積もり

| タスク | コスト |
|---|---|
| `PermissionDecl.mcp_install: bool` field 追加 | XS (= 1 行 + frozen dataclass invariant 確認) |
| `require_mcp_install()` resolver method | S (= 既存 `require_mcp()` patternで ~30 行) |
| `_is_config_approved("mcp_install")` 透過動作確認 | XS (= 既存実装で動く想定、 test 追加のみ) |
| approval key persist (= `mcp_install:<server_id>`) | XS (= 既存 `.reyn/approvals.yaml` で対応) |
| `startup_guard` 拡張 (= `decl.mcp_install` 検出時の pre-flight check) | S (= 既存 `startup_guard()` の `file_write` / `python` に並ぶ分岐) |
| Tier 2 test (= permission gate / scope tier integration) | S (= ~5-8 件) |
| `mcp_install` IR op 側で `require_mcp_install()` call | XS (= IR op 実装と同時、 ADR-0029 単独 cost には算入しない) |
| docs (= `permission-model.md` への記載追加) | S |
| **合計** | **SMALL-MEDIUM** (= 1-1.5 day、 IR op 実装 wave に bundle 推奨) |

---

## 5. 関連

- P3 (LLM 判断のみ) + P6 (events as audit truth): `docs/concepts/principles.md`
- 既存 permission system: `docs/concepts/permission-model.md`、 `src/reyn/permissions/permissions.py`
- ADR-0027 AuditSeal — `docs/deep-dives/decisions/0027-audit-seal-separation.md` (= `mcp_server_installed` event を将来 AuditSeal 入力ソースに統合)
- positioning doc — `docs/deep-dives/research/positioning/reyn-mcp-cli-shape.md` (= `mcp_install` IR op の design)
- 競合分析 — `docs/deep-dives/research/competitive/openclaw.md` (= ambient authority に対する差別化 motivation)

---

## 6. Acceptance criteria (= ADR Accepted 昇格時の確認 list)

- [ ] `PermissionDecl.mcp_install: bool` 追加、 既存 dataclass invariant 維持
- [ ] `require_mcp_install()` 実装 + Tier 2 test (= deny path / allow path / ask path / saved approval path)
- [ ] scope tier 各 layer (= user / project / local) で `permissions.mcp_install` declare が透過動作
- [ ] approval key `mcp_install:<server_id>` が `.reyn/approvals.yaml` に persist + 取り消し可能
- [ ] `mcp_install` IR op handler から `require_mcp_install()` 呼出 + `mcp_server_installed` event emit
- [ ] private registry override + `mcp_install: allow` の combination で「approved servers only」 policy が e2e で確認可能
- [ ] `--non-interactive` / `REYN_MCP_INSTALL_AUTO_APPROVE` 等の CI escape hatch 実装 + docs 記載
