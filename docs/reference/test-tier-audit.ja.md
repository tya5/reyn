# テスト Tier 監査ツール (`scripts/test_tier_audit.py`)

テストポリシー (`docs/deep-dives/contributing/testing.ja.md`) の 6 つのルールに対してテストファイルをチェックする AST ベースのリンター。ポリシーを「一度読む文書」から「PR ごとに機械的に適用される制約」に変えます。

## なぜこのツールが必要か

以前は、テストポリシーへの準拠はコードレビュー中にテスト diff を読んで確認していました。多くの違反はレビュアーが捕捉できていましたが、プロセスは手動で、レビュアーによってムラがあり、PR ループにレイテンシを加えていました。ポリシーを十分に習得していない新規コントリビューターは、レビューコメントが来るまで誰も気づかないまま MagicMock ベースのテストや Tier docstring の欠如を混入させることがありました。

`test_tier_audit.py` は最も頻繁な 6 種類の違反を数秒で検出可能にします。レビュアーがコードを見る前に、ローカルと CI の両方で実行できます。

## セットアップ

追加インストール不要 — スクリプトは標準ライブラリの `ast` モジュールを使用します:

```bash
python scripts/test_tier_audit.py [files or dirs]
```

## 検出ルール

リンターは 6 つのルールをチェックします。各ルールには深刻度と、テストポリシーに基づいた根拠があります。

### ルール 1 — Tier docstring の欠如 (ERROR)

すべてのテスト関数は、docstring の最初の行に Tier を宣言しなければなりません:

```python
def test_something():
    """Tier 3a: プランニングメッセージに対してルーターが正しいスキルを選択する。"""
    ...
```

Tier ラベル (`Tier 1`、`Tier 2`、`Tier 3a`、`Tier 3b`) は docstring 1 行目の先頭に記載する必要があります。docstring のない関数、または Tier ラベルで始まらない docstring はこのエラーをトリガーします。

**理由:** Tier ラベルがなければ、そのテストが存在すべきかどうか (Tier 4 = 書いてはいけない) や、どのコントラクトを assert しているかがわかりません。

### ルール 2 — フォーマットピンニング (Tier 4 ERROR)

`len(...) [<>=] N` 形式の式は、文字列・リスト・出力の正確な長さをピン留めします。長さのピン留めは Tier 4 違反です。テストされているコントラクトとは無関係な正当な理由で変わり得るアルゴリズムレベルの挙動をエンコードします。

```python
# 違反
assert len(result) == 5
assert len(output.splitlines()) < 100

# 許容
assert len(result) > 0      # 構造的: 空でないことの確認
```

**理由:** フォーマットピンニングは、空白の変更・出力の再フォーマット・有効なアルゴリズム改善で失敗する脆弱なテストを生みます。実際のコントラクトは何も違反していないのに。

### ルール 3 — プライベート状態の assertion (ERROR)

プライベート属性 (`obj._something`) への assertion は、クラスの公開コントラクトが公開していない実装の詳細に踏み込みます。

```python
# 違反
assert tracker._daily_tokens == 100
assert mgr._timers["c1"] is not None
```

**理由:** プライベート状態は公開 API の一部ではありません。それに assertion することは、挙動が変わっていなくても内部表現が変わるたびにテストが壊れることを意味します。代わりに公開サーフェスまたは `snapshot()` スタイルの読み取りメソッドを使用してください。

### ルール 4 — MagicMock / AsyncMock / patch の使用 (ERROR)

`unittest.mock.MagicMock`、`AsyncMock`、`patch` は禁止です。代わりに実際のインスタンスまたは `LLMReplay` フェイクを使用してください。

```python
# 違反
from unittest.mock import MagicMock, AsyncMock, patch

llm = MagicMock()
with patch("reyn.router.some_fn") as mock_fn:
    ...
```

**理由:** モックは実際の API コントラクトをバイパスします。任意の呼び出しを受け入れるモックは、実際のコラボレーターがその呼び出しを受け入れるかどうかを教えてくれません。モックはシステムの進化に伴って静かに腐敗します。実際のインターフェースが互換性のない形で変更されても通過し続けるためです。

### ルール 5 — 通常ディレクトリ内の有限ライフタイムテスト (WARNING)

docstring やコメントに `triggered_by`、`removed_by`、`scaffold_only` などのフレーズを含むテストは、有限の期待寿命があることを示しており、通常のテストディレクトリではなく `tests/scaffold/` に置く必要があります。

**理由:** `tests/scaffold/` は、特定のリファクタリング中に特定のリグレッションを捕捉するために存在し、リファクタリングが完了したら削除されるテストのための指定場所です。通常のスイートに混在させると、永続的なテストと一時的なテストの区別が見えにくくなります。

### ルール 6 — scaffold 外のスナップショット/ゴールデンテスト (ERROR)

`tests/scaffold/` 外でゴールデンファイルを書き込んだり読み込んだりするテスト (パターン: `golden`、`snapshot`、`.gold`、`.expected`) は、メインスイートにスナップショットテストを置くことを禁止するポリシーに違反します。

**理由:** メインスイートのスナップショットテストは出力フォーマットを永続的にロックし、メンテナンス負担を生み、出力フォーマットの変更で誤った失敗を引き起こします。有限ライフタイムテストと同じ理由で `tests/scaffold/` に属します。

## フラグ

| フラグ | 説明 |
|--------|------|
| `files/dirs` | 監査するファイルまたはディレクトリパス (位置引数) |
| `--strict` | 警告をエラーとして扱う。任意の発見で exit 1 |
| `--check RULE` | 指定したルールのみ実行 (繰り返し可。例: `--check rule1 --check rule4`) |
| `--quiet` | 発見ごとの詳細を抑制してサマリーのみ出力 |
| `--json` | 発見を JSON で出力 (1 発見 = 1 オブジェクト、改行区切り) |

## 出力例

### デフォルト出力

```
tests/test_router.py:42: [ERROR rule1] Missing Tier docstring: test_router_picks_skill
tests/test_router.py:87: [ERROR rule4] MagicMock usage: MagicMock
tests/test_util.py:12: [ERROR rule2] Format pinning: len(result) == 5

3 errors, 0 warnings
```

エラーがある場合は exit code 1、クリーンな監査の場合は exit code 0。

### `--quiet` 出力

```
3 errors, 0 warnings in 2 files
```

### `--json` 出力

```json
{"file": "tests/test_router.py", "line": 42, "severity": "ERROR", "rule": "rule1", "message": "Missing Tier docstring: test_router_picks_skill"}
{"file": "tests/test_router.py", "line": 87, "severity": "ERROR", "rule": "rule4", "message": "MagicMock usage: MagicMock"}
{"file": "tests/test_util.py", "line": 12, "severity": "ERROR", "rule": "rule2", "message": "Format pinning: len(result) == 5"}
```

## ワークフローとの統合

### pre-commit チェックとして

コミット前に変更したテストファイルに対して監査ツールを実行します:

```bash
python scripts/test_tier_audit.py tests/
```

またはコミットしようとしているファイルのみに対して:

```bash
git diff --cached --name-only | grep '^tests/.*\.py$' | \
  xargs python scripts/test_tier_audit.py
```

### PR レビューで

PR が新しいテストファイルを追加した場合、レビューの一部としてそのファイルに対して監査ツールを実行します:

```bash
python scripts/test_tier_audit.py tests/test_new_feature.py
```

### テストスイート内の既存違反の発見

スイート全体に対して `--quiet` で実行して件数を取得します:

```bash
python scripts/test_tier_audit.py tests/ --quiet
```

`--check rule4` を使って特定のルールに絞り込みます (例: コードベース全体の MagicMock 使用箇所を探す):

```bash
python scripts/test_tier_audit.py tests/ --check rule4
```

### ゼロトレランス CI での `--strict`

```bash
python scripts/test_tier_audit.py tests/ --strict
```

警告 (ルール 5) を含む任意の発見で exit 1 します。スイート全体がクリーンである必要がある CI パイプラインに適しています。

## 制限事項

監査ツールはヒューリスティックな指標であり、形式的な検証器ではありません:

- **誤検知は存在し、許容されます。** ルール 2 と 3 の正規表現パターンは、珍しいパターンの有効なコードをフラグする可能性があります (例: 列挙値の数を実際に気にするスキーマ検証テストの `len(enum_values) == 3`)。各発見を違反として扱う前に検査してください。
- **AST 解析のみ。** ツールはテストを実行せず、インポートを解決しません。間接インポートや動的構築で導入されたモックは検出できません。
- **ファイル横断解析なし。** 内部で MagicMock を使用するヘルパーに委譲するテストは、そのヘルパーファイルも監査対象でない限りフラグされません。

## 関連リソース

- [リプレイテストリファレンス](testing/replay.md) — `LLMReplay` フィクスチャとモックなしの Tier 3 テストの書き方
