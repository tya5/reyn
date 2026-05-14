# FP-0007: Agent 評価インフラ — P6 トレース export + スキル回帰評価

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Reyn は P6 イベントログを既に持っており、評価インフラとして活用できる構造が整っている。
本 FP では以下の 4 つを追加する:
(A) P6 イベントの外部評価ツール export adapter（Langfuse / OTLP / IETF Agent Audit Trail）、
(B) `reyn eval` コマンドによるゴールデンデータセット実行と CI ゲート、
(C) FP-0006 の `skill_version_hash` を使ったバージョン間回帰比較、
(D) フェーズから使える `judge_output` op（LLM スコアラー）。

---

## Motivation

### 業界の評価インフラ動向（2026-05 調査）

アカデミック領域では SWE-bench Verified（コーディング）と METR Time Horizon（安全性）が最多引用だが、
どちらも「本番挙動を反映しない」という構造的批判を受けている。
UC Berkeley（2026-04）は主要ベンチマーク 8 本で報酬ハッキングが可能であることを実証し、
Goodhart's Law（「測定指標になった途端に指標でなくなる」）の実例として広く引用されている。

エンタープライズ実態:
- **Braintrust**: CI/CD ゲート（PR ごとにスコア低下で merge ブロック）がデファクト
- **Langfuse**: OSS・自己ホスト可能 → 日本エンタープライズ（データ主権要件）で有力
- **IETF Agent Audit Trail**: `identity / timing / routing / parameters` の構造化ログ標準が策定中

### P6 イベントログが評価インフラの基盤になれる

Goodhart 問題への根本回答のひとつは「**どのバージョンで出したスコアか**のトレーサビリティ」。
FP-0006 で追加する `skill_version_hash` と P6 の append-only ログを組み合わせると、
「スキル v1 の 50 回実行 vs v2 の 50 回実行」の自動比較が追加実行ゼロで実現できる。

IETF Agent Audit Trail ドラフト（draft-sharif-agent-audit-trail）の必須フィールドは
Reyn の P6 イベント型で自然に表現できる:

| IETF フィールド | P6 イベント対応 |
|---|---|
| identity | chain_id / skill_name |
| timing | timestamp（全イベント共通）|
| routing | run_skill_started の state_dir |
| parameters | tool_executed の op + args |

---

## Proposed implementation

### Component A — P6 イベント export adapter（MEDIUM）

P6 イベントを外部評価ツールに送出するアダプタ。
P7 遵守のため、アダプタはスキル非依存の汎用 event schema を出力する
（アダプタは `type / timestamp / data` を読むだけで、スキル固有フィールド名を知らない）。

```python
# src/reyn/eval/export.py

class TraceExporter(Protocol):
    async def export(self, events: list[Event]) -> None: ...

class LangfuseExporter(TraceExporter): ...   # 自己ホスト可、日本エンタープライズ向け
class OTLPExporter(TraceExporter): ...       # OpenTelemetry 標準
class IETFAuditExporter(TraceExporter): ...  # IETF Agent Audit Trail draft 準拠
class FileExporter(TraceExporter): ...       # .reyn/traces/ へのローカル出力（デフォルト）
```

設定:

```yaml
# reyn.yaml
eval:
  exporters:
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://your-langfuse.example.com   # 自己ホスト URL
    - type: otlp
      endpoint: http://localhost:4317
    - type: file                                 # デフォルト（設定なしでも有効）
      path: .reyn/traces/
```

export のタイミング: スキル実行完了後に非同期で送出（実行パスに影響しない）。
失敗時はログに警告を出すのみ（P6 本体の書き込みは独立）。

### Component B — `reyn eval` コマンド（MEDIUM）

ゴールデンデータセットに対してスキルを実行し pass/fail とスコアを記録する
CI ゲート機構。

```
reyn eval run <skill_name> --dataset eval/golden.jsonl [--threshold 0.8]
reyn eval compare <skill_name> --from v1 --to v2        # バージョン間回帰比較
reyn eval report <skill_name>                            # 過去の eval 結果サマリー
```

**golden dataset フォーマット** (JSONL):

```jsonl
{"input": {"query": "..."}, "expected": {"summary": "..."}, "tags": ["smoke"]}
{"input": {"query": "..."}, "expected": {"summary": "..."}, "tags": ["regression"]}
```

**`reyn eval run` の動作**:

1. 各テストケースに対してスキルを実行（workspace は隔離）
2. `final_output` を `expected` と比較
   - `mode: exact` — JSON 完全一致
   - `mode: judge` — `judge_output` op（Component D）でスコア算出
3. 結果を `.reyn/eval-results/<skill>/<timestamp>.jsonl` に保存
4. 結果に `skill_version_hash` を記録（FP-0006 との接続）
5. pass rate が `--threshold` 未満なら **exit code 1** → CI gate として使用可能

**CI での使用例**:

```yaml
# .github/workflows/eval.yml
- run: reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8
```

### Component C — スキルバージョン回帰比較（SMALL）

FP-0006 の `skill_version_hash` を使って、
改善前後のスキルを同一データセットで比較する。
**追加の実行は不要** — P6 ログの集計のみで実現。

```
reyn eval compare my_skill --from v1 --to v2

  v1 (sha:abc123):  72% pass  (36/50)  2026-05-01 〜 2026-05-05
  v2 (sha:def456):  88% pass  (44/50)  2026-05-05 〜       ← current
  差分: +16pp  /  regression: なし
```

`reyn eval compare` は以下を参照:
- `.reyn/skill-versions/<name>/current` — カレントバージョン
- P6 `run_skill_started` イベントの `skill_version_hash` — 各バージョンの実行履歴
- `.reyn/eval-results/<skill>/` — 明示的な eval 実行の結果

### Component D — `judge_output` op（SMALL）

スキルの任意フェーズから使える LLM スコアラー op。
`run_and_eval` フェーズの eval ループとも、`reyn eval run` の比較エンジンとしても使用する。

**Control IR フォーマット**:

```json
{
  "op": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "以下の基準で 0.0〜1.0 でスコアリングしてください: ...",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

P7 遵守: rubric の内容は呼び出し側スキルが渡す。
OS 側の `judge_output` 実装は `target` パスの値と `rubric` 文字列を受け取るだけで、
スキル固有の評価基準を知らない。

`on_fail` の値は OS レベルの語彙のみ:
- `"transition"` — LLM が次フェーズを選択
- `"abort"` — スキル実行を abort
- `"continue"` — スコアに関わらず続行（スコアを workspace に記録するのみ）

結果は P6 に `tool_executed` (op=judge_output, score=0.72, passed=false) として記録される。

**CLAUDE.md の NEVER rule 遵守**:
`control-ir.md` と `OP_KIND_MODEL_MAP` は同一 PR で更新する（必須）。

---

## Hermes / Braintrust との比較

| 機能 | Braintrust | Hermes (未出荷) | Reyn（本 FP 後）|
|---|---|---|---|
| CI/CD eval gate | ✓ | — | ✓ (`reyn eval run`) |
| バージョン回帰比較 | ✓ | — | ✓ (FP-0006 + Component C) |
| 外部 export | Braintrust SaaS のみ | — | ✓ Langfuse / OTLP / IETF |
| 自己ホスト対応 | ✗ | — | ✓ (Langfuse セルフホスト) |
| IETF 準拠 | — | — | ✓ (Component A) |
| P7 遵守 | 該当なし | 該当なし | ✓ (OS はスキル固有知識なし) |

---

## Dependencies

- `src/reyn/events/events.py` — export の読み取り元（変更なし）
- `src/reyn/op_runtime/registry.py` — `judge_output` を `OP_KIND_MODEL_MAP` に追加
- `docs/reference/runtime/control-ir.md` — `judge_output` セクション追加（registry と同一 PR 必須）
- FP-0006（skill_version_hash）— Component C の前提。A / B / D は独立実装可能

前提 PR: なし（Component A / B / D は FP-0006 より先に実装可能）。
Component C のみ FP-0006 の `skill_version_hash` が前提。

---

## Cost estimate

**合計: LARGE**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: export adapter (Langfuse / OTLP / IETF / File) | MEDIUM | OTLP は well-spec'd。Langfuse は REST API が公開されている |
| Component B: `reyn eval run` + golden dataset runner | MEDIUM | workspace 隔離 + pass/fail 判定 + JSONL 出力 |
| Component C: バージョン回帰比較 | SMALL | P6 ログ集計のみ、新実行なし |
| Component D: `judge_output` op + registry + control-ir.md | SMALL | op 実装 + doc 更新 |
| テスト（Tier 1 / Tier 2） | SMALL | Component A の export contract + Component D の op contract |

ボトルネックは **Component B の workspace 隔離**（eval 実行が本番 workspace を汚染しない保証）
と **Component A の IETF フォーマット精度**（ドラフト仕様が変化している可能性）。

---

## Related

- `src/reyn/events/events.py` — P6 イベント基盤
- `src/reyn/op_runtime/registry.py` — OP_KIND_MODEL_MAP
- `docs/reference/runtime/control-ir.md` — op catalog（judge_output 追加対象）
- FP-0006 (`0006-skill-self-improvement.md`) — skill_version_hash（Component C の前提）
- `docs/deep-dives/research/landscape/hn-practitioner-voice-2026.md` — HN の観測性批判
- [IETF Agent Audit Trail draft](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/)
- [Langfuse OSS](https://langfuse.com/) — 自己ホスト対応評価プラットフォーム

---

## ユーザー向けドキュメント

FP-0007 ドキュメント wave として以下のユーザー向け doc が作成されました:

| ドキュメント | パス | 説明 |
|------------|-----|-----|
| コンセプト doc | `docs/concepts/evaluation.md` | アーキテクチャ、3 層モデル、競合比較 |
| コンセプト doc（JA） | `docs/concepts/evaluation.ja.md` | 日本語翻訳 |
| オペレーターガイド | `docs/guide/evaluation.md` | クイックスタート、export バックエンド、CI 連携、`judge_output` 使用例 |
| オペレーターガイド（JA） | `docs/guide/evaluation.ja.md` | 日本語翻訳 |
| CLI リファレンス | `docs/reference/cli/eval.md` | `reyn eval run` + `reyn eval report` フラグリファレンス（既存 eval.md に追記） |
| CLI リファレンス（JA） | `docs/reference/cli/eval.ja.md` | 日本語翻訳（既存 eval.ja.md に追記） |
