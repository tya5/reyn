# ADR-0027: AuditSeal を Events (P6) から分離する

**Status**: Proposed (2026-05-09)
**Track**: Architecture — 監査証跡設計の責務境界

---

## 1. Context

### Events (P6) の現在の責務

Reyn の Events システム（P6）は **operational** な責務を持つ：

- クラッシュリカバリのための WAL
- forward-replay による状態復元
- すべての状態変化の append-only 記録

この責務は明確に定義されており、OS の信頼性・完全性を支える基盤になっている。

### 監査証跡への要求

OSS リリース前の差別化戦略として、改ざん検知可能な監査証跡（ハッシュチェーン）の実装が検討されている。
競合（Hermes Agent Issue #487 未出荷、OpenClaw ambient authority）に対して Reyn が先行できる唯一の領域。

### 問題

ハッシュチェーンを Events に直接乗せると、**compliance** な責務が混入する：

| 責務 | 分類 | Events に属すか |
|---|---|---|
| WAL / state replay | operational | ✓ |
| append-only 記録 | operational | ✓ |
| 改ざん検知（hash chain） | compliance | ✗ |
| 誰が実行したか | compliance | ✗ |
| 何を指示したか | compliance | ✗ |
| モデル・バージョン記録 | compliance | ✗ |
| 外部承認記録 | compliance | ✗ |

compliance データは crash recovery に不要。Events に追加すると責務が汚染される。

さらに将来の監査要件（SOX 承認フロー、HIPAA アクセス記録など）は Events が記録しないデータを必要とする。
この場合、Events に追加するか AuditSeal の入力を散在させるかという二択に追い込まれる。

---

## 2. Decision

### 3 つの独立した概念として分離する

```
Events (P6)           → "何が起きたか"         operational
AuditContext (新規)   → "誰が・何のために・どの条件で"  compliance 専用
AuditSeal (新規)      → "それらが改ざんされていない"  証明
```

### AuditSeal の入力は拡張可能にする

「Events を読んで計算する」ではなく「登録された入力ソースから計算する」設計。

```
AuditSeal
  inputs:
    - Events (P6)        read-only
    - AuditContext       read-only
    - (将来の X)         read-only, 拡張可能
```

Events は AuditSeal を知らない。依存方向は一方向：

```
Events  ←── (read) ──┐
                      AuditSeal
AuditContext ←(read)─┘
```

### AuditContext の設計

Skill 実行開始時に OS が書き出す compliance 専用の記録。

```json
// audit/context/<run_id>.json
{
  "run_id": "abc123",
  "skill": "researcher",
  "invoked_by": "user@example.com",
  "original_request": "...",
  "model": "gemini-2.5-flash-lite",
  "model_version": "...",
  "config_hash": "sha256:...",
  "started_at": "2026-05-09T..."
}
```

### AuditSeal の設計

Skill 完了時に OS が生成・保持する seal。

```json
// audit/seals/<run_id>.json
{
  "run_id": "abc123",
  "skill": "researcher",
  "sealed_at": "2026-05-09T...",
  "event_count": 87,
  "chain_hash": "sha256:...",
  "prev_seal": "sha256:...",
  "context_hash": "sha256:..."
}
```

### 保持ポリシー

```yaml
# reyn.yaml
audit:
  seal_unit: skill          # skill / phase （将来拡張可能）
  retention:
    events_days: 30         # 詳細イベントは 30 日
    seals_forever: true     # seal は削除しない
  anchor:
    enabled: false          # デフォルト off
    provider: rfc3161       # 規制産業向けオプトイン
```

AuditSeal はデフォルト off のオプトイン機能として実装する。

---

## 3. Consequences

### ✓ 得られるもの

- **Events の責務が不変**: P6 は operational のまま。compliance 要件の変化が Events 設計に影響しない
- **将来の compliance 要件への対応**: AuditContext か新しい入力ソースを追加するだけ。Events に手を入れる圧力が発生しない
- **独立したライフサイクル**: AuditSeal を丸ごと外せる・差し替えられる・オフにできる
- **段階的実装**: Skill seal のみで OSS リリース → anchor（RFC 3161）はエンタープライズ tier で後追い

### △ トレードオフ

- AuditContext という新しい概念が増える（Events に乗せるより概念数が増える）
- Events と AuditContext の二重書き込みが発生する（ただし AuditContext は Skill 開始時の 1 回のみ）

---

## 4. 実装コスト見積もり

| タスク | コスト |
|---|---|
| AuditContext writer（Skill 開始フック） | SMALL |
| AuditSeal generator（Skill 完了フック） | SMALL |
| hash chain 計算・検証ロジック | SMALL |
| `reyn audit verify <run_id>` CLI | SMALL |
| `reyn.yaml` オプトイン設定 | SMALL |
| **合計** | **MEDIUM** |

---

## 5. 関連

- P6: Events as audit truth（`docs/concepts/events.md`）
- P23: WAL + forward-replay crash recovery（ADR-0001, ADR-0002）
- 競合分析: `docs/deep-dives/research/competitive/hermes-agent.md`（Issue #487 との対比）
- 戦略的優先事項: `docs/deep-dives/research/landscape/reyn-strategic-priorities.md`

---

## Implementation prerequisites

See [`0027-phase-1-decisions.md`](0027-phase-1-decisions.md) for the 5 user
judgment gates that must be confirmed before Phase 1a implementation begins:

1. Hash chain topology default (ADR-0027a)
2. config_hash scope (ADR-0027b)
3. Plan-mode seal boundary (ADR-0027c)
4. Writer failure defaults (ADR-0027d)
5. AuditContext schema scope
