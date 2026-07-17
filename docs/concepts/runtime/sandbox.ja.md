---
type: concept
topic: security
audience: [human, agent]
---

# サンドボックス

Reyn のサンドボックスレイヤーは、ワークフローが宣言したポリシーをカーネルレベルの強制に変換します — OS コードはどのワークフローが実行されているかを知りません。これは P3（OS がランタイムエンジン）と P7（OS コードにワークフロー固有の文字列を含めない）の直接的な適用です。ワークフローは *何が必要か* を宣言し、OS が *どのように強制するか* を選択します。

サンドボックスは [パーミッションモデル](../runtime/permission-model.md) を補完します。パーミッションモデルは op の実行前にディスパッチ時点で宣言スコープを強制し、サンドボックスはサブプロセスの実行中にシステムコールレベルで同じ境界を強制します。この 2 つのレイヤーは独立しており、組み合わせて使用します。

## `SandboxPolicy` フィールドリファレンス

`src/reyn/security/sandbox/policy.py` で定義。`sandboxed_exec` Control IR op のフィールドとして渡されます。

| フィールド | 型 | デフォルト | 意味 |
|---|---|---|---|
| `network` | `bool` | `false` | アウトバウンドネットワーク接続を許可 |
| `read_paths` | `list[str]` | `[]` | サブプロセスが読み取り可能なファイルシステムパス（glob パターンおよび `{{workspace}}` テンプレート可） |
| `write_paths` | `list[str]` | `[]` | サブプロセスが書き込み可能なファイルシステムパス（厳密なガード）。書き込みは読み取りを含む。`~` は展開される。 |
| `allow_subprocess` | `bool` | `false` | サンドボックス対象プロセスが子プロセスを生成することを許可。Linux (seccomp) / macOS (Seatbelt) で適用（off の時 `process-fork` を deny、対象自身の exec は `process-exec*` で動作）。 |
| `env_passthrough` | `list[str]` | `[]` | サブプロセスに引き渡す環境変数名（それ以外はすべて除去） |
| `timeout_seconds` | `int` | `60` | ウォールクロック上限（超過時にプロセスを強制終了） |

## バックエンド選択テーブル

`get_default_backend(config)` はプラットフォームとインストール済み extra に基づいてランタイムでバックエンドを選択します。`reyn.yaml` の `sandbox.backend` 設定キーで自動選択を上書きできます。

| プラットフォーム | 条件 | バックエンド | 備考 |
|---|---|---|---|
| macOS | < 26 | `SeatbeltBackend` | `sandbox-exec` 経由の SBPL プロファイル。上流で非推奨 — Apple が macOS 26 で削除予定。 |
| macOS | ≥ 26（将来） | `AppleContainerBackend` | 未実装（Component E、延期）。`NoopBackend` にフォールバック。 |
| Linux | カーネル ≥ 5.13 かつ `sandbox-linux` extra インストール済み | `LandlockBackend` + seccomp-BPF | `pip install reyn[sandbox-linux]` が必要。Landlock は**どの ABI でも**外向きネットワークを制限しない — pin された `landlock` パッケージがネットワークルール API を一切公開していないため。`network: false` は別の機構が担保し、backend が一度だけ WARN を出す。 |
| Linux | カーネル < 5.13 または `sandbox-linux` 未インストール | `NoopBackend` | 監査のみ、強制なし。 |
| その他 | 任意 | `NoopBackend` | 監査のみ、強制なし。 |

`NoopBackend` が使用される場合、Reyn は初回呼び出し時に一行 `WARN` をログ出力します。代わりにハードフェイルさせるには `sandbox.on_unsupported: error` を設定してください。

### 封じ込め self-test

バックエンドが選択されるのは、それが**このマシンで実際に deny を発火した**場合のみです — **主張している全ての軸で**。解決時に Reyn はそのバックエンド自身の wrap 経由で短いサブプロセスを起動し、実在するバックエンドなら必ず拒否すべき2つを試みます: `write_paths` の外への書き込みと、`allow_subprocess: false` 下でのプロセス spawn です。どちらかが成功してしまえば、そのバックエンドは主張どおりには封じ込めていない ∴ `sandbox.on_unsupported` が「バックエンドが存在しない」場合と同様に適用されます。

これが在るのは、**「機構が在る」と「機構が効く」が別の主張**であり、これまで前者しか検査されていなかったからです。バックエンドは、存在し import でき、それでいて完全に不活性であり得ます ∴ 存在だけを問う検査は、何も強制されていない状態で通ります。self-test は後者を、その主張を語っている当のホスト上で問います。

コストは1プロセスあたり probe 2回（短いサブプロセスが数個、数十ミリ秒）でバックエンド名に対してキャッシュされます。実際に real backend を解決する run だけが払い、sandbox に触れない run は払いません。chat 起動経路にも乗りません。

**なぜ probe が2つで、assertion 1つ増ではないか**: 2つの軸は**矛盾する policy を要求します** — write probe は syscall 層から自軸を隔離するために `allow_subprocess: true` を設定し、spawn probe はそのフラグ自体が主題なので `false` を要求します ∴ 1回の launch で両方は witness できません。そして2つの**検査**は独立に落ちます: Linux では write 境界は Landlock、spawn ゲートは seccomp ∴ filter が死んでいても path rule は動きます。write だけの検査は、そのホストを「封じ込め済み」と報告します。

**なぜ「通った方だけ残す」ではなく両方必須か**: **検査は分解できますが、保護は分解できません。** Landlock は通常の write を govern しますが **`chmod` 権限を一切持たず**、path ベースの `truncate` も handled set の外です ∴ **seccomp が無ければ、どちらも無統治**です。**Linux 6.8 で実測**（Landlock 発効・filter 不在）: `write_paths` 外のファイルへの `open()` は拒否され、**同じファイルへの `os.truncate()` は成功して中身を消しました**。それらの syscall を拒否しているのは、**allowlist に載せないことによる default-deny filter** です（`backends/seccomp.py` の `_EXCLUDED_UNGOVERNABLE` 参照）。∴ **Landlock-without-seccomp は「弱い sandbox」ではなく「首尾一貫しない sandbox」**であり、spawn probe が **filter が load したこと自体を witness する**ことで、その穴を閉じ続けます。

各 probe は deny の前に **positive control** を確立します: policy が *許可している* 操作が実際に起きたことを観測できなければ、通過ではなく「未 witness」と報告します。これが無ければ、何も実行しなかった wrap もまた禁じられたファイルを残さない ∴ 「何も起きなかった」が「deny が発火した」と全く同じに読めてしまいます。spawn probe は control をもう1つ持ちます（`allow_subprocess: false` 下で fork *しない* コマンドは動き続けねばならない）— その機構が default-deny の syscall filter であり、**全てを拒否する filter**と**`fork` だけを拒否する filter**は、それが無ければ判別不能だからです。

**覆っていない範囲**: probe が witness するのはファイルシステムの書き込み境界と、プロセス spawn ゲートで、いずれも command-level の wrap 経由です。ネットワークゲート、`read_deny_paths`、one-shot `run()` 経路の別の preexec ruleset を exercise するものではありません。通過したバックエンドは **deny を2つ発火した** — 主張するすべての deny の証明ではありません。

**macOS 26.3+ と `SeatbeltBackend`**: macOS 26.3 では `sandbox-exec` は継続出荷されています。SBPL プロファイルに `(import "bsd.sb")` と `(allow process-exec*)` を含めることでバックエンドが動作します。詳細は FP-0017 の post-dogfood fix landing notes（コミット `b477508`）を参照してください。

## `reyn.yaml` 設定

```yaml
sandbox:
  backend: auto        # auto | seatbelt | landlock | noop
  on_unsupported: warn # warn | error | ignore
```

- `backend: auto` — 現在のプラットフォームで利用可能な最適バックエンドを Reyn が選択（推奨）。
- `backend: noop` — 強制を明示的に無効化（イベント経由で監査するが強制は不要な CI 環境などで有用）。
- `on_unsupported: error` — 使用可能なバックエンドが無い場合にワークフローディスパッチを失敗させる（設定されたバックエンドがこのプラットフォームに存在しない場合に加え、**存在するが封じ込め self-test に失敗した場合**も含む）。強制が必須要件となる本番環境で使用。

## ワークフローでのサンドボックスポリシー宣言

ワークフローの `skill.md` で、`sandboxed_exec` を実行するフェーズに `sandbox:` ブロックを追加します:

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

`{{workspace}}` テンプレートは OS がランタイムでワークフローのワークスペースディレクトリに展開します。ワークフローオーサーは絶対パスをハードコードしてはなりません — ワークスペース相対パスにはすべて `{{workspace}}` を使用し、システムパス（`/usr/bin`、`/usr/lib` 等）については dylib ロードに必要なパスがバックエンドによって自動的に許可されます。

## 関連情報

- [FP-0017](../../deep-dives/proposals/0017-sandboxed-execution.ja.md) — 設計の根拠、コンポーネントの経緯、バックエンド実装の詳細。
- [Control IR: `sandboxed_exec`](../../reference/runtime/control-ir.ja.md#sandboxed_exec) — op スキーマとフィールドリファレンス。
- [パーミッションモデル](../runtime/permission-model.md) — サンドボックスがランタイムで補完するディスパッチ時点の宣言スコープ強制。
