# テストポリシー

Reyn は**自律性よりも予測可能性**を重視します（[プロジェクトビジョン](../concepts/principles.md)参照）。テストスイートもその方針を反映しています。**テストは OS を守る不変条件を保護するものであり、将来の進化に対するコストになってはなりません。**

このドキュメントがポリシーです。新しいテストを書く前に[判断フロー](#判断フロー)を確認し、ポリシーに合致しない既存テストは次に触れたタイミングでリファクタリングまたは削除します。

---

## 基本原則

> 良いテストは「壊れたときに何を示すか」で評価されます。「壊れにくい」ことは美徳ではありません。その性質はテストではなく、設計（P1–P8）と OS が担うものです。

テストが守るべきコントラクトや不変条件を明確に言語化できない場合、それは実装の固定（implementation pinning）を装った偽物です。実装の固定はテスト腐敗の最も一般的な原因です。実装が進化するたびにテストが壊れ、目的を再評価せずに修正され、スイートはフィードバック層ではなく摩擦層になっていきます。

---

## Tier モデル

`tests/` 内のテストはちょうど 1 つの Tier に属します。Tier は、テストが何を固定するか、誰が対象か、いつ変更すべきかを決定します。

### Tier 1 — Contract

**固定対象**: ユーザー／OSS コントリビューター／インテグレーションスクリプトが依存する外部境界。

- `reyn.yaml` スキーマ（必須フィールド、型、エラーケース）
- Events JSONL ペイロードスキーマ（監査・リプレイツールが依存）
- DSL コントラクト: `skill.md`・`phase.md`・`artifact.yaml` の必須セクション
- 各クラスタの `__init__.py` から再エクスポートされる公開 Python API

**粒度**: スキーマレベル。ユーザーが grep で検索するエラーメッセージトークン（例外クラス名、設定キー名など）を除き、特定の文言は固定しません。

**保留**: CLI 出力フォーマットは現リビジョンでは Tier 1 対象**外**です。CLI UX を見直し中であり、再設計後にコントラクトを追加する予定です。

### Tier 2 — OS invariant

**固定対象**: P1–P8 から導出される OS 自体の不変条件。

- LLM 出力コントラクト（`type=transition` ⇒ `next_phase` が非 null；`type=finish` ⇒ `next_phase` が null）
- **P1**: 自身の出力スキーマを含む Phase は OS に拒否される
- **P5**: Workspace チャネル外でフェーズ間に渡されたデータは次フェーズの入力として扱われない
- **P6**: Events ログをバイパスした状態変更が検出される（= すべての状態変更がイベントを生成する）

**粒度**: 不変条件。違反の方法を問わず、不変条件が破れたときにテストが失敗しなければなりません。

**目標件数**: 原則あたり 1〜2 ケース、合計 5〜10 件。これを超えるようであれば、実装テストが誤ったラベルで紛れ込んでいる可能性があります。

### Tier 3 — 振る舞いテスト（決定論的、Fake LLM）

**固定対象**: `litellm.acompletion` 境界で *Fake*（Mock ではない）LLM を通じて実行される、LLM 依存の OS パスの振る舞い。**Mock は禁止 — [Mock vs Fake](#mock-vs-fake)参照。**

#### Tier 3a — シングルコール・リプレイ（現在のスコープ）

1 テストあたり 1 回の LLM 呼び出し、1 フェーズ。代表例: 「この `ContextFrame` が与えられたとき、ルーターはインテントを X と分類する」。ドリフト検出は必須: 各エリアには意図的にズレたフレームで `MissingFixture` が発生することを確認するテストも含めます。

現在対象のエリア:
- `skill_router` — インテント分類（typical 1〜2、drift 1）
- `multi_hop` — chain_id 伝播、遅延返信（typical 1、drift 1）
- `skill_improver` — temp-copy ワークフロー + force_decide（typical 1、drift 1）
- `eval_builder` — ケースごとの criteria、ロールバックループ（typical 1、drift 1）

**目標件数**: 全エリア合計 6〜8 ケース（上限: 4 エリア × 2 ケース）。12 件以上はTier 4 に属すべき冗長なコーナーケース網羅の兆候です。

#### Tier 3b — エンドツーエンド・シナリオリプレイ（延期）

マルチフェーズセッション。Workspace と Events ストアの最終状態を assert します。現在は**スコープ外**（CLI／`ChatSession` ドライバの見直し中）。CLI 再設計後に追加予定です。

### Tier 4 — 書かない

以下に該当するテストはスイートに**追加しません**（技術的には pass するとしても）:

- **private state への直接 assert**（`tracker._daily_tokens == 100`）。`snapshot()` や公開 API を使う。
- **アルゴリズムの固定**（ソート順、dict のイテレーション順、内部キャッシュ構造）
- **コミットごとのリグレッション複製**。修正はコミットが担い、記録は PR の説明に残す。「この特定のバグ」のテストは、永久に成立すべき本物の不変条件でない限り追加しない。
- **LLM 出力の品質／意味的正確さ**（「この回答は役に立つか？」）。これは `eval` スキル（LLM-as-judge）の仕事であり、テストスイートの範囲外。[ポリシー外](#ポリシー外)を参照。
- **見た目のフォーマット固定**（空白、句読点、行数、カラーコード）
- **スナップショット／ゴールデンファイルテスト** — [スナップショットテストを採用しない理由](#スナップショットテストを採用しない理由)参照。限定的な例外は[Annex](#annex-スキャフォールディングテスト)にあります。
- **`litellm` への `unittest.mock` パッチ** — 代わりに[Fake](#mock-vs-fake)（`LLMReplay`）を使う。
- **カバレッジ目標**（例: 「行カバレッジ ≥ 80%」）。カバレッジは副作用であり、目標ではありません。PR のゲートにはしません。
- **デフォルトの TDD**。テストファーストは Tier 2 不変条件（コントラクトが実装前から明確な場合）に適しています。機能開発では「まず動かし、それから守る」を推奨します。未検証の設計を早期に凍結するテストは避けてください。

---

## 判断フロー

テストを書く前に、以下の設問に答えてください:

```
Q1. これが壊れたとき、誰が気づくか？
  A. 外部ユーザー／インテグレーター              → Tier 1（CLI 出力は現在延期）
  B. OS 自体（不変条件が崩れる）                → Tier 2
  C. 単一の LLM 呼び出しがドリフトする          → Tier 3a
  D. セッション全体がドリフトする               → Tier 3b（延期 — CLI 再設計を待つ）
  E. このコミットの著者だけが気づく             → 書かない — PR の説明で十分

Q2. これは将来の作業で摩擦になるか？
  - Skill の変更が触れる形状を固定している              → 書かない
  - リファクタリングでリネームされる private 名を固定     → 書かない
  - DSL が拡張予定の振る舞いを固定している              → 書かない

Q3. どのレベルで固定しているか？
  - 公開コントラクト／OS 不変条件レベル          → 書く
  - 実装レベル                                  → 書かない

Q4. LLM の意味的品質を測定しているか？
  → テストスイートのスコープ外。eval スキル（LLM-as-judge）を使う。
    参考: Anthropic の「regression eval」と「capability eval」の区別。
```

Tier 1〜3 に明確に位置づけられないテストは、ほぼ例外なく Tier 4 に属します。

---

## Mock vs Fake

LLM 依存テストは必ず Fake（`LLMReplay`）を使う必要があります。Mock は禁止です。

### 理由

Mock は関数を手書きのスタブに置き換えます:

```python
# 禁止
from unittest.mock import patch
with patch("litellm.acompletion", return_value=hand_built_dict):
    ...
```

これは実際の API コントラクトをバイパスします。`litellm` のシグネチャやレスポンス形状が変わったとき（例: LangChain が `__call__` を `invoke()` にリネームしたとき、エコシステム全体の Mock テストは pass し続けながら本番が壊れていました — Lincoln Loop, "Avoiding Mocks: Testing LLM Applications with LangChain in Django" 参照）、Mock テストはそれを検出できません。

Fake は実際の API サーフェスを通じてルーティングします。`LLMReplay` は `litellm.acompletion` をパッチしますが、記録済みデータから本物の `litellm.ModelResponse` を再構築します。シグネチャのドリフトは呼び出しサイト（TypeError、AttributeError）またはルックアップ時（`MissingFixture`）で検出されます。

### 使い方

```python
@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase():
    from reyn.testing.replay import REPLAY_DATETIME
    frame = ContextFrame(
        # ...
        current_datetime=REPLAY_DATETIME,  # 安定したキーに必須
    )
    response = await call_llm(model, frame, ...)
    assert response.data["type"] == "decide"
```

完全なセットアップ手順は[リプレイテストの書き方](#リプレイテストの書き方)を参照してください。

---

## スナップショットテストを採用しない理由

スナップショットテストは Phase／artifact／最終結果の構造的な出力を固定し、将来の実行との差分を確認します。これは**採用しません**。理由は以下の通りです:

1. **P1 に反する。** Phase は `input_schema` と instructions のみを宣言し、出力形状は次フェーズの `input_schema` や `final_output_schema` によって外部で決まります。スナップショットはその出力形状をテスト内に凍結し、P1 と相反します。
2. **Skill の進化で壊れる。** Skill の変更はすべて artifact に影響するため、スナップショットは定常的に更新されます。定常的な更新は「よさそうだから承認」という慣行に堕落し、スナップショットはガードとして機能しなくなります。
3. **差分レビューが雰囲気チェックになる。** 明確な不変条件がなければ、「スナップショット更新」のレビューは目視確認に劣化します。「期待される変更」と「リグレッション」を区別する原理的な方法がありません。
4. **Tier 2（OS 不変条件）がより適切なツール。** スナップショットが守ろうとするものは、多くの場合 LLM 出力構造や Workspace 状態に関する不変条件です。その不変条件を直接エンコードしてください。

業界文献もこれに沿っています: Coulman, *Snapshot Testing: Use With Care* (2016)；Hughes, *Why Snapshot Testing Sucks*；メタ分析 *Snapshot Testing in Practice: Benefits and Drawbacks* (Science of Computer Programming, 2024)。

限定的な例外が[Annex](#annex-スキャフォールディングテスト)に存在します（レガシーリファクタのキャラクタリゼーション用）。Coulman の元の枠組みに従います。

---

## Annex: スキャフォールディングテスト

これが bounded-life のテストを許可する唯一の場所です。**スキャフォールディングは Tier ではありません** — `tests/` スイート全体の原則を保つため、意図的に特殊ケースの例外として位置づけています。

### いつ使うか

既存エリアの大規模なリファクタリングやマイグレーションを行う際、作業中に意図しない振る舞いの変化を検出したい場合。スキャフォールディングテストは現在の振る舞いを固定し、リファクタリングが完了するまで存在し、完了時に削除されます。

### 必須メタデータ

```python
# scaffold: triggered_by="BudgetLedger を別のバッキングストアに置き換えたとき"
# scaffold: removed_by="新しいバッキングストアをランディングした PR"
def test_ledger_jsonl_format_during_migration():
    ...
```

トリガーは**観測可能**でなければなりません。「このコードパスが書き直されたとき」は可。「時間ができたら」や「Q4 以降」は不可。

### 削除の規律

トリガーイベントが発生した PR は、**同じ PR でスキャフォールディングテストも削除しなければなりません**。PR レビューでこれを確認します。

### 物理的な分離

スキャフォールディングテストは `tests/scaffold/` に配置します。このディレクトリ配下のファイルは、PR レビュー時にトリガーが古くなっていないか（トリガーイベントがすでに発生済みでないか）スキャンされます。

### スナップショットテストの例外

スナップショットテストは**レガシーリファクタのスキャフォールディング**（Coulman の「キャラクタリゼーションテスト」のユースケース）としてのみ許可されます。条件:
- `tests/scaffold/` に配置すること
- 具体的な `triggered_by`（リファクタ PR またはリリース）を持つこと
- リファクタがランディングされたタイミングで削除されること

これがコードベースにおけるスナップショットテストの唯一の認定用途です。

---

## ポリシー外

以下はテストスイートの外に属します:

- **LLM 出力の意味的品質。** 「このレスポンスは本当に役に立つか？」は `eval` スキル（LLM-as-judge）の仕事です。テストスイートは「構造は正しいままか」を問います — Anthropic はこれを *regression eval* と呼んでいます。品質は *capability eval* であり、別の場所に属します。
- **モデル比較ベンチマーク**（gemini vs claude vs gpt）。`eval` スキルや専用のベンチマークツールを使ってください。
- **本番トラフィックの監視／アラート。** `events.jsonl` と外部監視を使ってください。これはオペレーショナルインフラであり、テストではありません。

---

## リプレイテストの書き方

> Tier 3a テストの参考資料。最も一般的なコントリビューションの形式です。

### ボイラープレート

```python
import pytest
import asyncio
from reyn.llm.llm import call_llm
from reyn.schemas.models import ContextFrame
from reyn.testing.replay import REPLAY_DATETIME


@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_my_phase_classifies_as_x():
    """Tier 3a: skill_router が chitchat 入力を finish と分類する。"""
    frame = ContextFrame(
        current_phase="classify",
        # ... その他のフィールド ...
        current_datetime=REPLAY_DATETIME,   # 必須
    )

    result = asyncio.get_event_loop().run_until_complete(
        call_llm(
            model="gemini-2.5-flash-lite",
            frame=frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            phase_role="chat_router",
        )
    )

    assert result.data["type"] == "decide"
    assert result.data["control"]["decision"] == "finish"
```

### フィクスチャパス

パスは `tests/` からの相対パスです。例: `"fixtures/llm/skill_router/chitchat.jsonl"`。

### フィクスチャの記録

**初回**（フィクスチャファイルが存在しない）: conftest がこれを検出し、自動的に記録モードに切り替えます。稼働中の LLM が必要です（ローカル開発では `localhost:4000` の LiteLLM プロキシ — メモリの `project_local_env.md` 参照）。

```bash
python -m pytest tests/test_replay_my_area.py -v
# フィクスチャが tests/fixtures/llm/my_area/my_scenario.jsonl に書き込まれます
```

**意図的なプロンプトのドリフト後**: フィクスチャを削除して再記録します:

```bash
rm tests/fixtures/llm/my_area/my_scenario.jsonl
REYN_LLM_RECORD=1 python -m pytest tests/test_replay_my_area.py -v
```

### ドリフト検出 — 各エリアで必須

Tier 3a の各エリアには、フィクスチャがカバーしていないフレームを意図的に構築し、`MissingFixture` が発生することを assert するテストが 1 つ必要です。これが偶発的なプロンプトのドリフトを検出する仕組みです。

```python
@pytest.mark.replay("fixtures/llm/my_area/my_scenario.jsonl")
def test_wrong_input_raises_missing_fixture():
    """Tier 3a drift detection: instructions / candidate_outputs の変更は
    フィクスチャの再記録が必要。さもなければテストが大きな音で失敗する。"""
    frame = ContextFrame(
        current_phase="classify",
        instructions="これは意図的にフィクスチャに含まれていない",
        current_datetime=REPLAY_DATETIME,
    )
    from reyn.testing.replay import MissingFixture
    with pytest.raises(MissingFixture):
        asyncio.get_event_loop().run_until_complete(call_llm(...))
```

### フィクスチャフォーマット

JSONL、1 行 1 レコード:

```json
{"key": "<sha256>", "model": "gemini-2.5-flash-lite", "prompt_preview": "...", "response": {...}}
```

- `key` — `SHA256(model + canonical_json(messages))`
- `prompt_preview` — 最後のメッセージの先頭 200 文字（grep 用）
- `response` — `litellm.ModelResponse.model_dump()`、リプレイ時に再構築

### モンキーパッチのライフサイクル

`tests/conftest.py` は `@pytest.mark.replay` を持つテストに対して `LLMReplay` をインストールし、`try/finally` で復元します。マーカーを持たないテストは本物の `litellm.acompletion` を参照します。`tests/test_replay_skill_router.py` の `test_no_monkeypatch_leak` で検証済みです。

---

## テストの実行

```bash
# すべてのテスト
python -m pytest tests/ -v

# リプレイテストのみ
python -m pytest tests/test_replay_*.py -v

# OS 不変条件テストのみ（Tier 2）
python -m pytest tests/test_os_invariants.py -v

# 強制記録モード（稼働中の LLM が必要）
REYN_LLM_RECORD=1 python -m pytest tests/ -v
```

---

## 新しい OS 機能のカバレッジチェックリスト

LLM 依存の OS パスを新たに追加する場合:

- [ ] 代表的なハッピーパスに対する Tier 3a テスト 1 件
- [ ] コーナーケース（force_decide、エラーパス、境界値）に対する Tier 3a テスト 1 件
- [ ] ドリフト検出テスト（`MissingFixture` の assert）1 件
- [ ] P1–P8 の不変条件から導出される機能であれば、Tier 2 テスト 1 件を追加
- [ ] 公開コントラクト（yaml スキーマ、Events ペイロード、DSL セクション）を変更する場合は Tier 1 テストを更新／追加
- [ ] `current_datetime=datetime.now()` を使っていないことを確認 — 常に `REPLAY_DATETIME` を使う
- [ ] 各テストの docstring 一行目に Tier の明記（例: `"""Tier 3a: ..."""`）
