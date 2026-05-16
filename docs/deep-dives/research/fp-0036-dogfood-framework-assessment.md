---
type: research
topic: FP-0036 Dogfood Scenario Framework — 実装アセスメント
status: stable
last_updated: 2026-05-17
audience: [implementer, architect]
---

# FP-0036 実装アセスメント — Dogfood Scenario Framework

GitHub issue: #44  
関連: `dogfood-discipline.md` / `src/reyn/testing/replay.py` / `docs/feature-map.md`

---

## 1. 現状把握

FP-0036 で "今あるもの" として列挙された 3 つの確認結果:

| 要素 | 確認結果 |
|---|---|
| `dogfood/scenarios/*.yaml` | ✅ 存在。`long_session_v1.yaml` (7 multi-turn) / `fp_0011_0012_retest.yaml` / `fp_0011_narration.yaml` — いずれも `expected` なし、runner なし |
| `reyn eval` (FP-0007) | ✅ 着地済み。`--threshold` / exit-code 2 / `reyn eval compare` パターンが確立されている |
| `src/reyn/dogfood/` モジュール | ❌ 未存在 |

**追加で確認した既存資産（FP-0036 本文に言及なし）:**

| 資産 | 場所 | 備考 |
|---|---|---|
| `LLMReplay` | `src/reyn/testing/replay.py` | `REYN_LLM_RECORD=1` / SHA-256 keyed JSONL fixtures / mature |
| `judge_output` op | `src/reyn/op_runtime/judge_output.py` | Component C reply verifier の backend として直接再利用可 |
| `dogfood-discipline.md` | `docs/deep-dives/contributing/` | 1155 行。4-band outcome / 9+ principles / 4-batch 構造 — Component B runner の UX の根拠文書 |
| `docs/feature-map.md` | `docs/` | 全機能 (17 op + stdlib + CLI + config) を網羅。Component D の covers タグ source-of-truth |
| `reyn web` A2A endpoint | `scripts/` | stdout piping より高信頼度のチャット実行 driver として使用可 |

---

## 2. コンポーネント別アセスメント

### Component A — Scenario YAML schema + loader (MEDIUM)

**新規性**: 既存 YAML との互換性が問題になる。

現行の `fp_0011_0012_retest.yaml` は以下のキーを使用:
```yaml
metadata:
  name: ...
  spike_id: ...
  retest_floor_n: 10
scenarios:
  - id: s-fp11-1-...
    user_prompt: |
      ...
    verification_axes: [D1, D2, ...]
```

FP-0036 提案の新スキーマ (`type: dogfood_scenario_set` / `covers: [...]` /
`expected.reply|events|artifacts`) とは別形式。

**推奨**: 新スキーマファイルは別ディレクトリ
(`dogfood/scenarios/regression/`) に置き、既存ファイルはレガシー
（= input-only、runner 非対応）として扱う。Loader は `type:` フィールドで
ディスパッチし、legacy 形式は読み込み可能・実行不可とする。

**設計注意点**:
- `multi_turn` シナリオ（= `prompts: [...]` 形式）を新スキーマでも保持すること。
  `input` フィールドを `str | list[str]` として multi-turn に対応させる。
- `expected` の `per_turn_expected` 対応は MVP でスキップして良い（FP-0036 本文も示唆）。

---

### Component B — Runner CLI (MEDIUM)

**最重要設計決定: チャット実行 driver の選択**

FP-0036 は具体的な driver を指定していない。3 つの選択肢がある:

| 方式 | 信頼度 | 実装コスト | 備考 |
|---|---|---|---|
| A: `subprocess` + stdin pipe | 低 | 低 | 既存 `dogfood_g4_spike.py` の方式。イベントログ取得に worktree 操作が必要 |
| B: `reyn web` A2A endpoint | 中 | 中 | HTTP + structured response。`dogfood-discipline.md` §6 推奨 |
| C: In-process (= `reyn eval` と同様) | 高 | 高 | `ChatSession` 直接呼び出し。replay に最適だが session.py 依存が深い |

**推奨: MVP は方式 B (`reyn web`)、長期は方式 C**。

方式 B の理由:
- `reyn web` は既に存在し、dogfood 用途で実績がある
- stdout piping (A) よりイベントログへのアクセスが確実
- in-process (C) は isolation が弱く、失敗時のワークスペース汚染リスクがある

**イベントログ取得の課題**:
Runner はチャットセッションの `.reyn/events/*.jsonl` を取得して
Component C (events verifier) に渡す必要がある。方式 B の場合:
- セッションごとに専用 `--state-dir` を指定し、run 完了後に読み出す。
- `run_id` = `datetime + scenario_id` で一意なセッション dir を作れる。

**CLI UX 推奨**:
```
reyn dogfood run <yaml>           # 基本実行
reyn dogfood run <yaml> --n 3     # N回実行 (安定性バンド)
reyn dogfood run <yaml> --replay  # LLMReplay fixture 使用
reyn dogfood report <run_id>      # 4-band サマリー
reyn dogfood coverage             # feature-map 未カバー表示
reyn dogfood compare <base> <cand>  # 回帰 diff
```

MVP scope: `run` + `report` のみ。`compare` は Component E。

---

### Component C — Verifier triad (SMALL)

**reply verifier**:
- `judge` → `judge_output` op を内部で dispatch。**FP-0007 D の着地済み実装を直接再利用。**
- `substring` / `exact` / `regex` → 純関数。trivial。
- コスト注意: `judge` は LLM call を消費する。`--replay` なしで N=5 実行すると
  `N × scenario 数 × 1 judge call` のコストがかかる。LLMReplay (Component F) が
  cost 削減の鍵。

**events verifier**:
- `must_emit`: イベントの `type` + `count` + `payload` パターンを JSON Schema で照合。
- `must_not_emit`: 同イベントタイプの不在確認。
- sequence: ordered subsequence（= 単調増加インデックスで実装可）。
- **P6 イベントログは append-only JSONL** — シンプルな行ストリームで対処できる。

**artifacts verifier**:
- `present: true` は workspace snapshot 内の artifact ファイル存在確認のみ。
- `fingerprint` (SHA256) はオプション。MVP でスキップ推奨（= fixture 更新コストが高い）。

**outcome 合成ルール**（FP-0036 "worst-case" を具体化）:

```
refuted > blocked > inconclusive > verified
```

3 つの verifier の中で最も重篤な outcome がシナリオ outcome になる。
この strict ルールは初期シナリオ設計が甘い場合に `refuted` rate が高くなるが、
それ自体がシグナル（= 設計フィードバック）なので意図通り。

---

### Component D — Feature-map coverage matrix (SMALL)

**feature-map.md のパースに関する警告**:

FP-0036 は `docs/feature-map.md` をパースすると書いているが、
同ファイルは人間向け markdown（Mermaid + table 混在）のため機械パースが脆い。

**推奨**: 別途 `dogfood/feature-map-index.yaml` を作成する。

```yaml
# dogfood/feature-map-index.yaml
features:
  - path: os-core/phase-engine/act-decide-loop
    label: Act/Decide loop
    reference: docs/concepts/principles.md
  - path: control-ir/embed
    label: embed op
    reference: docs/reference/runtime/control-ir.md
  # ... (feature-map.md のテーブルから抽出)
```

`reyn dogfood coverage` はこの YAML + 各シナリオの `covers:` タグを突き合わせる。
`feature-map.md` のメンテと `feature-map-index.yaml` の同期は手動で良い（低頻度更新）。

この方法の利点:
- `feature-map.md` を壊さずに coverage 機能を実装できる
- feature-map に新項目が追加されたときに coverage gap が自動的に visible になる

---

### Component E — Baseline compare (SMALL)

`reyn eval compare` の実装パターンをそのまま移植可能:
- `--threshold FLOAT` → 4-band の `verified` 率の閾値
- exit code 2 → `verified` 率が閾値を下回る
- Brier score = `Σ (predicted_p - actual_binary)^2 / N` — calibration 指標

**追加推奨**: `--scenario-filter <id>` で特定シナリオの diff に絞る機能。
回帰確認時に「どのシナリオで変化したか」をピンポイントで見るユースケースが多い。

---

### Component F — LLMReplay fixture integration (SMALL)

`LLMReplay` の API は既に:
```python
replay = LLMReplay(fixture_path, mode="replay")
replay.install()  # litellm.acompletion を monkeypatch
```

Component F の実装は薄い wrapper のみ:
```python
# src/reyn/dogfood/replay.py
def fixture_path_for(scenario_id: str, run_dir: Path) -> Path:
    return run_dir / "fixtures" / f"{scenario_id}.jsonl"
```

**重要**: `reyn dogfood run --replay` は `REYN_LLM_RECORD` を無視して
既存 fixture を使う。Fixture 不在時は `MissingFixture` を `blocked` outcome
に変換する（= runner が fixture の有無を `blocked` の条件として扱う）。

Fixture 鮮度管理:
- Fixture JSONL の先頭行に `{"schema_version": 1, "recorded_at": "..."}` を追加推奨。
- Runner が schema version mismatch を検出したら警告 + re-record 促進。

---

## 3. 実装順序の推奨

依存グラフ:

```
F (LLMReplay wrapper)
│
A (Schema + Loader) ──→ C (Verifiers) ──→ B MVP (run + report)
                                          │
D (feature-map-index.yaml) ──────────────→ B coverage subcommand
                                          │
E (Baseline compare) ────────────────────→ B compare subcommand
```

**推奨 PR 分割**:

| PR | 内容 | 依存 |
|---|---|---|
| PR-1 | Component F + A + C | なし |
| PR-2 | Component B MVP (`run` + `report`) | PR-1 |
| PR-3 | Component D (`dogfood/feature-map-index.yaml` + `coverage` subcommand) | PR-2 |
| PR-4 | Component E (`compare` subcommand) | PR-2 |
| PR-5 | シナリオ authoring wave (別 wave) | PR-2 着地後 |

PR-1 → PR-2 の順番が最重要。F+A+C を先に出すことで:
- Verifier ロジックを単体テスト可能な状態で確認できる
- Runner 実装時に verifier の API が固まっている

---

## 4. Open design points に対する推奨回答

FP-0036 が "resolve before implementation" とした 4 点:

### 1. シナリオセットの粒度

**推奨: per-category YAML + coverage index**。

```
dogfood/scenarios/regression/
  chat_router.yaml         # chat-router/* covers
  control_ir_ops.yaml      # control-ir/* covers
  stdlib_skills.yaml       # stdlib-skill/* covers
  permissions.yaml         # permissions/* covers
  rag_memory.yaml          # rag/* / memory/* covers
  sandbox.yaml             # sandbox/* covers
  multi_agent.yaml         # multi-agent/* covers
  README.md                # index of all sets + coverage %
```

1 ファイルに全シナリオを集めると PR レビューが困難になる。
feature-map のセクション構造に合わせることで coverage gap が自然に可視化される。

### 2. Baseline の保存場所

**MVP: `.reyn/dogfood/` (per-developer)**。

Rationale: 初期段階では baseline の "正解" が developer 間で異なる可能性が高い。
共有 baseline は LLM モデルバージョン / API レートの差異で false regression が増える。
`reyn dogfood run --baseline <label>` でローカルに named baseline を作成し、
将来 CI で共有する場合は `--export <path>` でエクスポートするパスを用意する。

### 3. Fixture の鮮度管理

**推奨: schema version + リリースタグ時 re-record 推奨**。

- Fixture JSONL にメタデータ行 (`schema_version`, `recorded_at`, `reyn_version`) を追加
- Runner は schema version mismatch を `blocked` outcome + 警告として扱う
- 完全自動 re-record は避ける（= 意図せず behavior 変化を fixture に焼き付けるリスク）
- `reyn dogfood run --record <set>` で明示的に再録できる形にする

### 4. Outcome 合成

**推奨: events/artifacts verifier が reply judge より優先**。

Rationale: events / artifacts は deterministic な binary check（= pass/fail）なのに対して、
reply judge は probabilistic（= LLM score）。Binary check が fail した場合に
probabilistic check を `inconclusive` に倒す設計にすると、
P6 event log による客観的証拠を優先でき、余計な LLM コストを節約できる。

```
合成ルール:
  events/artifacts いずれかが refuted  → シナリオ refuted
  events/artifacts いずれかが blocked  → シナリオ blocked
  events/artifacts 全 pass + judge inconclusive → シナリオ inconclusive
  events/artifacts 全 pass + judge verified     → シナリオ verified
```

---

## 5. リスクと懸念事項

### R1: イベントキャプチャの信頼度（中リスク）

`reyn web` driver 経由でも、セッション用 `--state-dir` を正確に受け渡せないと
events.jsonl が空になる。MVP 段階で `--state-dir` のハンドシェイクを確実に
テストする必要がある（= Tier 2 OS invariant test: events file 生成確認）。

### R2: judge_output コスト（低リスク）

N=5 実行 × 20 シナリオ = 100 judge calls/suite run。LLMReplay fixture が
ない状態での初回フル run がコスト高。**対策**: Fixture 作成を
`PR-1 のマージ後・PR-2 前` に一度 `--record` 走行でまとめて行う。

### R3: feature-map-index.yaml のメンテ遅延（低リスク）

新機能追加時に `feature-map.md` は更新されても `feature-map-index.yaml` が
更新されないと coverage が不正確になる。**対策**: PR テンプレートに
「新機能追加時は `dogfood/feature-map-index.yaml` も更新する」を追記する。

### R4: LLMReplay のモデル変更耐性（中リスク）

`LLMReplay` の fixture key は `model + canonical_json(messages)` の SHA-256。
モデルバージョン更新時（例: `gemini-2.5-flash-lite` → `gemini-2.5-flash-lite-001`）に
全 fixture が miss する。**対策**: Fixture 内に `model_class` (= `light`/`standard`/`strong`)
を別途記録し、`MissingFixture` を `blocked` として報告する設計を Component F に組み込む。

---

## 6. "framework PR" の最小スコープ定義

FP-0036 は "framework PR を先に出し、scenario authoring は別 wave" としている。
Framework PR の Done 条件を明確化する:

**必須 (= framework PR に含める)**:
- `dogfood/scenarios/regression/` ディレクトリ + README
- `src/reyn/dogfood/{__init__, scenarios, verifiers/, replay}.py` (PR-1)
- `reyn dogfood run <yaml>` + `reyn dogfood report <run_id>` (PR-2 MVP)
- `dogfood/feature-map-index.yaml` (D)
- Tier 1/2 テスト (verifiers 単体 + schema loader)

**defer (= 別 PR)**:
- `reyn dogfood coverage` (D — feature-map-index が確定してから)
- `reyn dogfood compare` (E)
- シナリオ authoring (PR-5)

---

## 7. FP-0035 との関係

FP-0035 (Sandbox/Permission LLM Communication) は「dogfood-driven 設計スタディ」と
明示しており、FP-0036 の framework が着地してからでないと評価できない。
FP-0035 の Phase 1 (評価フレームワーク確立) は FP-0036 の PR-2 着地が前提条件。

依存関係:
```
FP-0036 PR-2 (runner MVP) → FP-0035 Phase 1 評価 → FP-0035 Phase 2 実装
```

---

## まとめ

| 項目 | 評価 |
|---|---|
| P7 compliance | ✅ OS 層変更なし、`src/reyn/dogfood/` は完全独立 |
| 既存資産の再利用率 | 高 — `LLMReplay` / `judge_output` / `reyn eval compare` パターン / `feature-map.md` すべて再利用可 |
| MVP コスト | MEDIUM — PR-1 + PR-2 で 1-1.5 day |
| 最大リスク | イベントキャプチャの `--state-dir` ハンドシェイク |
| 最優先 open design point | **driver 選択 (B: `reyn web` 推奨)** と **feature-map-index.yaml の新規作成** |

FP-0036 は設計として健全。依存が全て着地済みで実装可能な状態。
開始前に driver 選択と feature-map-index.yaml 作成方針を確定させること。
