---
type: concept
topic: architecture
audience: [human, agent]
---

# Python `safe` モード — 「ambient sources only」

`mode: safe` の python ステップは、
[preprocessor](preprocessor.md) と [postprocessor](postprocessor.md) が使う
サンドボックス化された Python 関数呼び出しです。本ページは、ステップを
`safe` モードに置いてよいかを判断する際に作者が依拠できる単一の性質と、
allowlist にどの stdlib モジュールが入っているかを文書化します。

> **改名注**: `safe` は以前 `pure` と呼ばれていました (FP-0014 で改名)。
> 対になるモード `unsafe` は以前 `trusted` と呼ばれていました。

## 形式的性質: ambient sources only

`mode: safe` の python ステップの出力は、以下のみから決定されます。

1. 入力 artifact (= 明示的な `args_from` 依存)
2. **Ambient sources**、定義は以下:
   - **Clock**: `time`、`datetime.now()` — システム壁時計 + 単調クロック
   - **Entropy**: `random`、`secrets` — `/dev/urandom` ベースの PRNG / CSPRNG
   - **同梱静的データ**: `zoneinfo` — Python に同梱された IANA TZ データベース

ファイルシステム、ネットワーク、subprocess、環境変数へのアクセスは
`mode: safe` ステップから構文的に到達不能です。 allowlist がこれを
import 時に強制します。

## 「ambient」 vs 「pure」 の言葉の選択

文字通りの 「純粋関数」 解釈では、`time.time()` と `random.random()` は
どちらも隠れたグローバル状態に依存するため除外されます。しかし除外すると、
すべての `mode: safe` ステップが clock / entropy を明示的な入力 artifact として
受け取らなければならなくなり — 非実用的で、作者の期待とも合いません。

「ambient sources」 という表現は、ソースが well-defined であり、かつ
オペレータ / 攻撃者の制御下にない限り、一部の非決定論は許容可能であることを
認めています。

## 単一の性質

> **`mode: safe`**: python ステップの出力は、入力 artifact と **ambient
> sources** — 壁時計、エントロピーストリーム、Python に同梱された stdlib の
> 静的データ — のみから決定される。 ファイルシステム、ネットワーク、
> プロセス、 環境変数へのアクセスは構文的に到達不能。

これは AST バリデータと subprocess サンドボックスが共同で強制する性質です。
新しい stdlib モジュールを allowlist に追加して安全か知りたいときは、
こう問えば十分です。 *すべての public 呼び出しが {入力, 時計, エントロピー,
同梱静的データ} だけで満たせるか?* yes なら ambient。 Python を再デプロイ
せずにオペレータが変更できるもの (ファイル、環境変数、ネットワーク) が
必要なら ambient ではありません。

## Ambient vs non-ambient 早見表

| クラス | 意味 | 許可される例 |
|-------|---------------|------------------|
| **Inputs** | ステップに渡される artifact | `artifact` 引数 |
| **Clock** | OS が見る現在時刻 | `time.time()`, `datetime.now()` |
| **Entropy** | OS が提供する乱数 | `random`, `secrets` |
| **同梱静的データ** | Python install と一緒に出荷されるファイル | `zoneinfo` (tz データベース) |

これら以外はすべて **non-ambient** で、 `safe` モードから外れます。

| Non-ambient クラス | 除外理由 | 代表モジュール |
|-------------------|--------------------|-----------------|
| ファイルシステム読み | オペレータ管理 state を読む | `pathlib`, `glob`, `os.path`, `open` |
| ファイルシステム書き | オペレータ可視 state を変える | `open(..., "w")`, `shutil` |
| ネットワーク | 外部・無限・遅延あり | `urllib`, `requests`, `socket`, `http` |
| プロセス制御 | サンドボックス外への副作用 | `subprocess`, `os.system`, `os.fork` |
| 環境変数 | ステップが宣言していないオペレータ tunable な入力 | `os.environ`, `os.getenv` |
| 動的コード | 他のすべての check を bypass する | `eval`, `exec`, `compile`, `__import__` |

## I/O に見えるのに許可されている理由

allowlist のいくつかのエントリは一見 I/O のように見えます。それらは I/O が
**ambient** — Python install と一緒に出荷されているか、管理されたカーネル
ファシリティから提供されている — であり、オペレータ tunable な workspace
state ではないために残されています。

- **`zoneinfo`** はタイムゾーンファイルを読みますが、それらは Python
  (あるいはホストの `tzdata` パッケージ) に同梱されたものです。同じ
  Python install であれば答えは決定的です。ステップがこの経路で
  オペレータ編集ファイルを観測することはできません。
- **`random`** と **`secrets`** は OS エントロピーストリームから引きます。
  非決定的ですが ambient です — ステップはこれを使って workspace state を
  読むことはできず、 新しいビットを生成できるだけです。
- **`time`** / **`datetime.now()`** は壁時計を読みます。非決定的ですが
  ambient で副作用なしです。
- **`hashlib`** / **`hmac`** は引数に対する純粋計算です。

共通する性質: これらはどれも *{入力, 時計, エントロピー, 同梱静的データ}*
だけから満たせます。 ステップが入力としてすでに受け取っていない
オペレータの filesystem / network / 環境に関する情報を、これらを使って
ステップが学ぶことはできません。

## allowlist の読み方

[`src/reyn/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/kernel/_python_allowlist.py)
の各エントリには、なぜ contract を満たすかを説明する短い inline コメントが
付いています。 カテゴリは以下のとおりです。

- `# ambient: ...` — 上記の形式的性質に該当 (clock / entropy / 同梱静的データ)
- `# restricted to ...` — モジュールは許可するが純粋な操作のみ
  (例: `pathlib.PurePath` のみ、`Path.read_text()` 不可)
- `# pure` — ambient アクセスなし (例: `math`、`re`)

## 現在許可されている stdlib モジュール

| モジュール | Ambient クラス |
|--------|---------------|
| `math`, `cmath`, `statistics`, `decimal`, `fractions`, `numbers` | 純粋計算 |
| `string`, `re`, `textwrap`, `unicodedata` | 純粋計算 |
| `json`, `base64`, `binascii`, `hashlib`, `hmac` | 純粋計算 |
| `collections`, `itertools`, `functools`, `operator`, `copy` | 純粋計算 |
| `enum`, `dataclasses`, `typing`, `abc` | 純粋計算 |
| `random` | entropy |
| `secrets` | entropy |
| `time` | clock |
| `datetime`, `calendar` | clock |
| `zoneinfo` | 同梱静的データ (tz データベース) |

正本リストは
[`src/reyn/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/kernel/_python_allowlist.py)
です。 プロジェクトは `reyn.yaml` の `permissions.python.allowed_modules`
で拡張できますが、拡張の基準は同じ「ambient sources only」性質です。

## 拡張方法 — `safe` で足りないとき

ステップが ambient でない能力 — オペレータが選んだファイルの読み込み、 HTTP
サービス呼び出し、 プロセス spawn、 `os.environ` の参照 — を必要とする場合、
allowlist への新規エントリ追加を要求しては **いけません**。 代わりに
preprocessor / postprocessor チェーンで `type: run_op` を使います。

`run_op` が正規の escape hatch です。

- OS の op runtime を通り、独自の permission gate がかかります。
- 能力が import 経由の暗黙ではなく明示的です (例: `read_file`,
  `http_request`)。
- 1 呼び出しあたり event log を残すので、 non-ambient アクセスの監査
  story が壊れません ([P6](principles.md#p6-events-are-the-audit-truth))。

要するに、 **`safe` python は入力 + ambient sources に対する
決定論的-ish な計算のためのもの。 それ以外はすべて `run_op`** です。

## See also

- [Reference: preprocessor DSL](../reference/dsl/preprocessor.md) — `python` ステップの宣言
- [Reference: postprocessor DSL](../reference/dsl/postprocessor.md) — finish 側の同じ DSL
- [Concept: preprocessor](preprocessor.md) — deterministic-split の説明
- [Concept: postprocessor](postprocessor.md) — finish 側のミラー
- [`src/reyn/kernel/_python_allowlist.py`](https://github.com/tya5/reyn/blob/main/src/reyn/kernel/_python_allowlist.py) — 正本リスト
