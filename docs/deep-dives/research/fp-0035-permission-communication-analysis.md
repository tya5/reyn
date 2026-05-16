---
type: research
topic: FP-0035 Sandbox/Permission LLM Communication — 設計空間分析
status: stable
last_updated: 2026-05-17
audience: [implementer, architect]
---

# FP-0035 設計空間分析 — Permission Communication パターン

GitHub issue: #39  
関連: `src/reyn/permissions/permissions.py` / `src/reyn/tools/universal_catalog.py` / `src/reyn/chat/router_tools.py`

---

## 1. 現状の実装サーベイ

FP-0035 が "4 pattern を evaluate する" と言っているが、実装を読むと
**permission type ごとにすでに異なるパターンが採用されている**。
まず現状を正確に把握する。

### 1.1 PermissionError メッセージの質（コード直接確認）

| Permission type | メッセージ品質 | LLM への actionability |
|---|---|---|
| `file.read` | 高 | YAML スニペット付き。`skill.md` に何を書けばよいかを完全指示 |
| `file.write` | 高 | 同上 |
| `shell` | 高 | `permissions: shell: true` を追記するよう指示 |
| `mcp` | 高 | `permissions: mcp: [server_name]` + FP-0016 エージェント許可リスト説明 |
| `mcp_install` | 高 | PermissionDecl 未宣言 / 設定 deny / ユーザー拒否を別メッセージで区別 |
| `index_drop` | 高 | `permissions.index_drop: true` への誘導 |
| `web.fetch` | **低** | "web fetch denied by config (web.fetch: deny)" or "web fetch denied" のみ |
| `web.search` | **低** | "web search denied by config (web.search: deny)" のみ |
| `sandboxed_exec` | n/a | Permission gate なし — `SandboxPolicy` は LLM が op 内で宣言 |
| budget | n/a | `PermissionError` でなく `budget_exceeded` event + TUI 表示 |

**観察**: `file` / `shell` / `mcp` / `index_drop` は "Pattern B+" として成熟している。
`web` 系は Pattern B だが error message が non-actionable（LLM が何もできない）。

### 1.2 カタログの permission 反映状況

`universal_catalog.py` の `visible_categories()` は `"web"` を常に含む。
`router_tools.py` の web_search / web_fetch は FP-0022 以降カタログから外れず、
実行時に `PermissionResolver` で deny される。

**結果**: `web.search: deny` 設定時でも LLM は `web` カテゴリを `list_actions` で見え、
`invoke_action("web__web_search", ...)` を試行 → non-actionable エラー → wasted call、
の完全な Pattern B 失敗ケースが成立する。

---

## 2. Permission type × 認証レイヤー マトリクス

FP-0035 が見落としている重要な軸: **「誰が deny を出しているか」** によって
最適パターンが異なる。

| 認証レイヤー | 例 | LLM が回復可能か | 最適パターン |
|---|---|---|---|
| **Config-deny** (reyn.yaml) | `web.fetch: deny` | ❌ 不可 (operator 設定) | **D: カタログから除外** |
| **Approval-pending** (未承認) | 未知パスへの file.read | ✅ ask_user で申請可 | **B+: actionable error** |
| **Decl-missing** (スキル未宣言) | `shell` 未宣言 | ✅ 実行前に設計修正可 | **B+: YAML スニペット付き error** |
| **Agent-allowlist** (FP-0016) | `allowed_mcp` scope 外 | ❌ 設計レベルの制約 | **B (現状維持)** |

この分類から **universal な 1 pattern** は存在しない。
正しいフレームワークは「permission type ごとに適切なパターンを選択する」。

---

## 3. Permission type 別の推奨パターン

### 3.1 file.read / file.write — **現状維持 (Pattern B+)**

現在の実装は既に最適に近い:
- CWD 配下はデフォルト許可 → 多くのケースでエラーが出ない
- CWD 外への試行 → YAML スニペット付き actionable error
- LLM はエラーを受けて `ask_user` で追加承認を申請できる

変更不要。ただし dogfood で "wasted call rate" を測定し、
restrictive scope (e.g., `read_paths: [src/]` のみ許可) で
Pattern A (upfront disclosure) が有意に改善するかを確認すべき。

**dogfood での検証ポイント**:
- Pattern A: SP に `## Permitted paths: src/` を追加
- Pattern B (現状): 試行 → PermissionError フィードバック
- 比較指標: wasted call 数、plan correctness

### 3.2 web.search / web.fetch — **config-deny は Pattern D、approval は Pattern B+**

**現状の問題**:
- `web.search: deny` でも `list_actions` に `web` カテゴリが出続ける
- Non-actionable error のみ → LLM が何もできない

**推奨設計**:

```
Config check:
  web.search: deny  → list_actions の web カテゴリから web_search を除外 (Pattern D)
  web.fetch: deny   → list_actions から web_fetch を除外 (Pattern D)

Session approval:
  web.fetch: allow not set → 試行 → InterventionBus で approve/deny prompt (Pattern B+)
  web.search は read-only → config deny 以外は常に allow (現状通り)
```

`visible_categories()` または `list_actions` ハンドラに
`permission_resolver._is_config_denied("web.search")` を反映させる改修が必要。

**実装コスト**: SMALL。`universal_catalog.py` の `visible_categories()` か
`router_tools.py` の Section E に config deny check を追加するだけ。

### 3.3 shell — **現状維持 (Pattern B+)**

`permissions: shell: true` の未宣言は Decl-missing パターン (actionable)。
Config deny (`--allow-shell` なし) は approval-pending (InterventionBus)。
メッセージ品質は十分。

### 3.4 mcp — **現状維持 (Pattern B+)**

FP-0016 で Confused Deputy 対策 (`allowed_mcp` allowlist) まで組み込まれており、
error message は区別済み。変更不要。

### 3.5 sandboxed_exec — **"Declarative" パターンとして維持**

`SandboxPolicy` は LLM が op 内で毎回宣言する設計:
```json
{
  "kind": "sandboxed_exec",
  "argv": ["python", "script.py"],
  "policy": {"network": false, "read_paths": ["src/"], "write_paths": [".reyn/"]}
}
```

これは A/B/C/D どれとも異なる **"Declarative" パターン**: LLM が制約を宣言し、
OS がそれに従って backend を選択・実行する。Permission gate が事前に走らないため
wasted call が発生しない設計になっている。

**変更不要**。ただし `describe_action("exec__sandboxed_exec")` のレスポンスに
`SandboxPolicy` フィールドの説明を補足すると LLM の policy 選択精度が上がる可能性がある
(= Phase 2 候補)。

### 3.6 budget — **Pattern A を維持・強化**

Budget は "permission" でなく "resource limit" であり、試行で回復できない。
現状: `budget_warn` イベント + TUI 表示 (Pattern A partial)。

**推奨**: budget_remaining が threshold を下回った時点で SP に
`## Budget: ¥{remaining} / ¥{limit} (XX% remaining)` を追加する。
これにより LLM が大量 web_fetch を控える、あるいは ask_user で方針確認するきっかけになる。

**実装コスト**: SMALL。`router_system_prompt.py` に budget section を条件付きで追加。

---

## 4. FP-0035 の 4 パターン評価（改訂版）

FP-0035 が挙げた 4 パターンを 実装結果から改訂評価:

| Pattern | 適用すべき permission type | 実装状態 |
|---|---|---|
| **A. Upfront disclosure** | budget (残量 warning 時) | 未実装 (SP budget section なし) |
| **B. Trial-and-error** | file / shell / mcp / index_drop (approval-pending / decl-missing) | ✅ 実装済・成熟 |
| **C. On-demand introspection** | sandboxed_exec policy description | 部分的 (describe_action あり、policy detail なし) |
| **D. Structural gating** | web.search / web.fetch (config-deny 時) | ❌ **未実装 — 最大の gap** |
| **"Declarative"** (新パターン) | sandboxed_exec | ✅ 実装済 |

---

## 5. 最大 gap の特定

**最優先で対処すべき gap: web ops の config-deny 時 Pattern D 未実装**

1. `web.search: deny` + `web.fetch: deny` はオペレーター設定であり LLM が回避不可
2. しかし `list_actions` の `web` カテゴリには変化なし
3. LLM が試行 → non-actionable error → plan が止まる

これは FP-0035 の "dogfood-first" を待たず修正できる **cost:SMALL のバグ的な改善**。
独立した issue として早期対処を推奨する。

---

## 6. Phase 1 評価フレームワーク設計（FP-0036 向け入力）

FP-0035 の Phase 1 は「evaluation framework establish」。
FP-0036 framework が着地したら以下の dogfood シナリオセットで評価する:

### 測定指標

| 指標 | 計測方法 |
|---|---|
| Wasted call rate | events log の `permission_denied` 直前の tool call 数 |
| Plan correctness | judge verifier (rubric: "did LLM achieve the stated goal?") |
| SP token cost | `context_built` event の `system_prompt_tokens` field |
| Error recovery rate | `permission_denied` 後に LLM が ask_user or 別路線に転換できたか |

### シナリオ設計案

```yaml
# dogfood/scenarios/regression/permissions.yaml
scenarios:
  - id: file_cwd_boundary
    covers: [permissions/file-read, permissions/trial-and-error]
    input: "src/reyn/schemas/models.py を読んで MCPIROp の定義を教えて"
    expected:
      events:
        must_emit: [{type: routing_decided}]
        must_not_emit: [{type: permission_denied}]

  - id: web_config_deny
    covers: [permissions/web-config-deny, permissions/structural-gating]
    input: "最新の Anthropic ニュースを web 検索して教えて"
    # reyn.yaml: web.search: deny
    expected:
      reply:
        kind: judge
        rubric:
          - LLM informs user web search is disabled
          - LLM does NOT attempt web_search after being told it's unavailable
      events:
        must_not_emit: [{type: permission_denied}]  # Pattern D: no wasted call

  - id: restrictive_file_scope
    covers: [permissions/file-read, permissions/upfront-vs-trial]
    input: "tests/ ディレクトリの全テストを一覧して"
    # reyn.yaml: file.read: deny for outside CWD subdir
    expected:
      events:
        # Pattern B: 1 wasted call ok, recovery via ask_user
        must_emit: [{type: ask_user_presented}]
```

---

## 7. 推奨アクション

| Priority | アクション | コスト | FP |
|---|---|---|---|
| P0 | web config-deny 時に `list_actions` から除外 (Pattern D) | SMALL | 新 issue 発行 |
| P1 | budget SP section (Pattern A, 残量 threshold 以下で表示) | SMALL | FP-0035 Phase 2 候補 |
| P2 | FP-0036 framework 着地後に dogfood scenario set `permissions.yaml` を authoring | MEDIUM | FP-0036 scenario wave |
| P3 | `describe_action("exec__sandboxed_exec")` に SandboxPolicy フィールド説明を追加 (Pattern C 強化) | SMALL | FP-0035 Phase 2 候補 |
| defer | Pattern A の full file scope disclosure | — | dogfood 結果次第 |

---

## 8. 新 issue 提案: web config-deny Pattern D

上記 P0 の内容を独立 issue として発行:

- `visible_categories()` または `list_actions` handler が `permission_resolver` から
  `web.search: deny` / `web.fetch: deny` の設定を読み取り、
  該当 action を除外する
- `REYN_LLM_RECORD` なしで再現可能なシンプルな改修
- FP-0034 §D14 の visibility gate (exec / search) と同じ設計思想

---

## 関連

- `src/reyn/permissions/permissions.py` — PermissionResolver + error messages
- `src/reyn/tools/universal_catalog.py` — `visible_categories()` / `list_actions`
- `src/reyn/chat/router_tools.py` — Section E web tools (L510–)
- FP-0034 (#36 closed) — B23-PRE-1 SP simplify、trial-and-error 採択の経緯
- FP-0036 (#44) — dogfood framework、Phase 1 評価の前提
- `docs/deep-dives/research/fp-0036-dogfood-framework-assessment.md` — 評価フレームワーク設計
