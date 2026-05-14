# FP-0026: Op/Permission クロスレイヤー整合性 — `reyn skill validate` + allowed_ops からの permission 要求自動導出

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

フェーズ frontmatter の `allowed_ops`、スキル frontmatter の `permissions`、`reyn.yaml` のランタイム設定という 3 つの宣言面が、現在はクロスレイヤーの整合性チェックなしに独立して存在している。非整合は実行時まで発見されず、発見タイミングが異なる 2 種類の失敗モードとスキル作者への高い認知負荷を生んでいる。本 FP では (A) `reyn skill validate` CLI コマンドによるオーサリング時の整合性チェック、(B) スキルロード時の OS レベル警告、(C) Tier 2-3 op の `op_catalog` 説明へのガイダンス注記（メタスキルのオーサリングギャップを解消）を提案する。

---

## Motivation

### 3 つの宣言面、整合性チェックなし

スキル作者は関連する permission / op 情報を 3 箇所に分けて書く必要がある:

```
フェーズ frontmatter  →  allowed_ops: [shell, file]
スキル frontmatter   →  permissions: { shell: { ... } }
reyn.yaml           →  shell: allow
```

OS は各レイヤーを独立して検証するが、クロスレイヤーの整合性チェックは行わない。内部矛盾のあるスキル（フェーズが `shell` を使う、スキルに `permissions.shell` がない）は個別チェックをすべて通過し、最初の実行時にのみ失敗する。

### 発見タイミングが異なる 2 種類の失敗モード

**モード A — フェーズ宣言エラー**（高速フィードバック）:
LLM が `available_control_ops` にない op を出力 → OS バリデーションで即 REJECTED。
明確・決定論的・副作用発生前に検出。

**モード B — permission 宣言エラー**（遅い・想定外）:
LLM が `allowed_ops` に含まれる op を出力 → フェーズバリデーションを通過 → 実行時に `PermissionError`。同じ `control_ir` バッチの先行 op がすでに副作用を起こした後に失敗する場合もある。

モード B を `reyn skill validate` で事前に検出することが本 FP の核心。

### スキル作者の認知負荷

`allowed_ops`（フェーズレベル）と `permissions`（スキルレベル）は、異なる角度から同じことを述べているように見える。「LLM 出力を制御する」vs「OS 実行を認可する」という区別は OS の内部構造の理解を前提とし、スキル作者に surface されておらず、ギャップを埋めるツールもない。

### メタスキルの op_catalog 混乱

メタスキル（`skill_builder`、`skill_improver`、`skill_importer`）は他スキルのフェーズ frontmatter を生成するために全 `op_catalog` を参照する。メタスキルの LLM は生成先フェーズに `allowed_ops: [shell]` と書くことができるが、生成先スキルに `permissions.shell` 宣言が必要なことを示すシグナルが現在の `op_catalog` 説明にない。結果として生成されたスキルが実行時に PermissionError を起こす。

---

## Proposed implementation

### Component A — `reyn skill validate` CLI（SMALL）

`reyn skill` の新サブコマンド:

```
reyn skill validate <skill_name>
reyn skill validate --all      # インストール済みスキルを全検証
```

バリデーションロジック:

```python
def _requires_declaration(op_kind: str) -> bool:
    """Tier 2-3 op（スキルレベル宣言が必要な op）かどうか。"""
    return op_kind in {"shell", "mcp", "file_outside_zone"}  # Tier モデルに合わせて拡張

def validate_skill(skill: Skill) -> list[ValidationIssue]:
    issues = []
    required = {
        op_kind
        for phase in skill.phases.values()
        for op_kind in phase.allowed_ops
        if _requires_declaration(op_kind)
    }
    declared = set(skill.permissions.keys())

    for op_kind in required - declared:
        issues.append(ValidationError(
            code="missing_permission_declaration",
            message=(
                f"フェーズが allowed_ops に '{op_kind}' を含んでいますが、"
                f"スキルに permissions.{op_kind} 宣言がありません — "
                f"実行時に PermissionError になります。"
            ),
        ))
    for op_kind in declared - required:
        issues.append(ValidationWarning(
            code="unused_permission_declaration",
            message=(
                f"スキルが permissions.{op_kind} を宣言していますが、"
                f"どのフェーズも allowed_ops に含めていません。"
            ),
        ))
    return issues
```

**Tier 0-1 op はチェック対象外** — `run_skill`、`ask_user`、`web_fetch`、`web_search` は Tier モデル（FP-0022）に従いスキルレベルの宣言が不要。

**統合先:**
- `reyn skill install <name>` — 自動的に validation を実行。警告はノンブロッキング、エラーは目立つ表示
- `reyn skill validate <name>` — 明示チェック。エラーがあれば exit code 1（スキルリポジトリの CI ゲートとして利用可能）

### Component B — スキルロード時の整合性警告（SMALL）

スキルロード時（LLM 呼び出し前）に OS が有効 permission 要求セットを計算し、宣言と乖離があれば警告:

```python
# src/reyn/kernel/skill_loader.py（または相当箇所）
effective_required = {
    op_kind
    for phase in skill.phases.values()
    for op_kind in phase.allowed_ops
    if _requires_declaration(op_kind)
}
declared = set(skill.permissions.keys())
missing = effective_required - declared
if missing:
    logger.warning(
        "スキル '%s': allowed_ops が %s を含んでいますが permission 宣言がありません — "
        "実行時に PermissionError になります。"
        "`reyn skill validate %s` で詳細を確認してください。",
        skill.name, missing, skill.name,
    )
```

モード B の発見タイミングを「最初の実行時」から「スキルロード時」に前倒しする。

### Component C — Tier 2-3 op の op_catalog 説明への注記（SMALL）

`src/reyn/kernel/control_ir_executor.py` の Tier 2-3 op spec の `description` に注記を追加:

```python
ControlIROpSpec(
    kind="shell",
    description=(
        "シェルコマンドを実行する。"
        "スキル frontmatter に permissions.shell が必要。"
        "allowed_ops にこの op を含むフェーズを持つスキルは "
        "permissions.shell を宣言しないと実行時に PermissionError になる。"
    ),
    example=...,
)
```

メタスキルの LLM が `op_catalog` を読んで `allowed_ops: [shell]` を生成する際、生成先スキルに `permissions.shell` も必要なことを同時に認識できる。

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/cli/skill.py` | `validate` サブコマンド追加（Component A） |
| `src/reyn/kernel/skill_loader.py` | ロード時整合性警告追加（Component B） |
| `src/reyn/kernel/control_ir_executor.py` | `_requires_declaration()` ヘルパー追加; Tier 2-3 op 説明更新（Component C） |
| `docs/deep-dives/contributing/skill-authoring.md` | 3 レイヤーの説明 + `reyn skill validate` のドキュメント |

---

## Dependencies

なし。既存の permission と `allowed_ops` インフラをそのまま利用。
FP-0022（Tier モデル）が Tier 2-3 op の定義を提供; `_requires_declaration()` はそのモデルと同期して維持する。

---

## Cost estimate

| コンポーネント | タスク | コスト |
|---|---|---|
| A | `reyn skill validate` CLI + バリデーションロジック | SMALL |
| B | ロード時整合性警告 | SMALL |
| C | Tier 2-3 op の op_catalog 説明更新 | SMALL |
| **合計** | | **SMALL** |

3 コンポーネントはすべて additive — 既存の動作変更なし。

---

## Verification

1. `allowed_ops: [shell]` はあるが `permissions.shell` がないスキル → `reyn skill validate` が `missing_permission_declaration` を報告し exit code 1
2. `permissions.shell` はあるが `shell` を使うフェーズがないスキル → `unused_permission_declaration` 警告、exit code 0
3. 非整合スキルへの `reyn skill install` → 警告表示（インストールはブロックしない）
4. 非整合スキルのスキルロード → "will PermissionError at runtime" と `reyn skill validate` ヒントを含む警告ログ
5. メタスキルが新フェーズ向けに `allowed_ops: [shell]` を生成する → op_catalog 説明を受けた LLM が生成先スキルの `permissions.shell` も生成する
6. Tier 0-1 op（`web_fetch`、`ask_user`）→ `permissions` 宣言がなくてもバリデーションエラーなし

---

## Related

- FP-0022 (`0022-permission-tier-model.ja.md`) — Tier 0-3 モデル（どの op が宣言を必要とするかを定義）
- `src/reyn/kernel/runtime.py` — `_build_context_frame()`（`allowed_ops` から `available_control_ops` をフィルタリング）
- `src/reyn/kernel/control_ir_executor.py` — `available_ops()`（op 種類と説明を定義）
- `docs/concepts/permission-model.md` — permission レイヤーのコンセプト文書
