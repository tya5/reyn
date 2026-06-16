---
type: concept
topic: security
audience: [human, agent]
---

# サンドボックス

Reyn のサンドボックスレイヤーは、スキルが宣言したポリシーをカーネルレベルの強制に変換します — OS コードはどのスキルが実行されているかを知りません。これは P3（OS がランタイムエンジン）と P7（OS コードにスキル固有の文字列を含めない）の直接的な適用です。スキルは *何が必要か* を宣言し、OS が *どのように強制するか* を選択します。

サンドボックスは [パーミッションモデル](../runtime/permission-model.md) を補完します。パーミッションモデルは op の実行前にディスパッチ時点で宣言スコープを強制し、サンドボックスはサブプロセスの実行中にシステムコールレベルで同じ境界を強制します。この 2 つのレイヤーは独立しており、組み合わせて使用します。

## `SandboxPolicy` フィールドリファレンス

`src/reyn/security/sandbox/policy.py` で定義。`sandboxed_exec` Control IR op のフィールドとして渡されます。

| フィールド | 型 | デフォルト | 意味 |
|---|---|---|---|
| `network` | `bool` | `false` | アウトバウンドネットワーク接続を許可 |
| `read_paths` | `list[str]` | `[]` | サブプロセスが読み取り可能なファイルシステムパス（glob パターンおよび `{{workspace}}` テンプレート可） |
| `write_paths` | `list[str]` | `[]` | サブプロセスが書き込み可能なファイルシステムパス |
| `allow_subprocess` | `bool` | `false` | サブプロセスが子プロセスを生成することを許可 |
| `env_passthrough` | `list[str]` | `[]` | サブプロセスに引き渡す環境変数名（それ以外はすべて除去） |
| `timeout_seconds` | `int` | `60` | ウォールクロック上限（超過時にプロセスを強制終了） |

## バックエンド選択テーブル

`get_default_backend(config)` はプラットフォームとインストール済み extra に基づいてランタイムでバックエンドを選択します。`reyn.yaml` の `sandbox.backend` 設定キーで自動選択を上書きできます。

| プラットフォーム | 条件 | バックエンド | 備考 |
|---|---|---|---|
| macOS | < 26 | `SeatbeltBackend` | `sandbox-exec` 経由の SBPL プロファイル。上流で非推奨 — Apple が macOS 26 で削除予定。 |
| macOS | ≥ 26（将来） | `AppleContainerBackend` | 未実装（Component E、延期）。`NoopBackend` にフォールバック。 |
| Linux | カーネル ≥ 5.13 かつ `sandbox-linux` extra インストール済み | `LandlockBackend` + seccomp-BPF | `pip install reyn[sandbox-linux]` が必要。ABI v4 以上でネットワークポートルールも有効。 |
| Linux | カーネル < 5.13 または `sandbox-linux` 未インストール | `NoopBackend` | 監査のみ、強制なし。 |
| その他 | 任意 | `NoopBackend` | 監査のみ、強制なし。 |

`NoopBackend` が使用される場合、Reyn は初回呼び出し時に一行 `WARN` をログ出力します。代わりにハードフェイルさせるには `sandbox.on_unsupported: error` を設定してください。

**macOS 26.3+ と `SeatbeltBackend`**: macOS 26.3 では `sandbox-exec` は継続出荷されています。SBPL プロファイルに `(import "bsd.sb")` と `(allow process-exec*)` を含めることでバックエンドが動作します。詳細は FP-0017 の post-dogfood fix landing notes（コミット `b477508`）を参照してください。

## `reyn.yaml` 設定

```yaml
sandbox:
  backend: auto        # auto | seatbelt | landlock | noop
  on_unsupported: warn # warn | error | ignore
```

- `backend: auto` — 現在のプラットフォームで利用可能な最適バックエンドを Reyn が選択（推奨）。
- `backend: noop` — 強制を明示的に無効化（イベント経由で監査するが強制は不要な CI 環境などで有用）。
- `on_unsupported: error` — 設定されたバックエンドが利用不可の場合にスキルディスパッチを失敗させる。強制が必須要件となる本番環境で使用。

## スキルでのサンドボックスポリシー宣言

スキルの `skill.md` で、`sandboxed_exec` を実行するフェーズに `sandbox:` ブロックを追加します:

```yaml
# skill.md の抜粋
phases:
  - name: run_script
    instructions: |
      分析スクリプトを実行する。
    sandbox:
      read_paths:
        - "{{workspace}}/input"
      write_paths:
        - "{{workspace}}/output"
      network: false
      timeout_seconds: 120
```

`{{workspace}}` テンプレートは OS がランタイムでスキルのワークスペースディレクトリに展開します。スキルオーサーは絶対パスをハードコードしてはなりません — ワークスペース相対パスにはすべて `{{workspace}}` を使用し、システムパス（`/usr/bin`、`/usr/lib` 等）については dylib ロードに必要なパスがバックエンドによって自動的に許可されます。

## 関連情報

- [FP-0017](../../deep-dives/proposals/0017-sandboxed-execution.ja.md) — 設計の根拠、コンポーネントの経緯、バックエンド実装の詳細。
- [Control IR: `sandboxed_exec`](../../reference/runtime/control-ir.ja.md#sandboxed_exec) — op スキーマとフィールドリファレンス。
- [パーミッションモデル](../runtime/permission-model.md) — サンドボックスがランタイムで補完するディスパッチ時点の宣言スコープ強制。
