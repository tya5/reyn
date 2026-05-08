# Batch 2 (Real) — Retrospective

> regression net は green。 ただし multi-agent UX には attractor 形 HIGH bug
> が 2 件残る。 カレーレシピが user に届く日は、 まだ来ていない。

---

## 前提 — batch 2 の起点と事前 prediction

batch 1 (practice) が 3 scenario で 11 件の finding を出した後、 A5 wave で
HIGH 8 件を fix した。 Tier 2 invariant は 660 passed。 batch 2 はその
「test suite green = e2e green なのか?」 という問いへの答えを出すための
本格 batch だった。

事前 prediction (5 件):

| Scenario | 予想 | 現実 | 結果 |
|---|---|---|---|
| S1 (text 要約) | 起動するハズ、 50% で再発も | invoke した。 ただし skill 名を幻覚 → B2-M1 | 外れ (方向は当たり) |
| S2 (MCP) | config 正しければ動くハズ | Run 2 成功 ✅、 ただし B2-H3 を直前で踏む | 当たり (条件付き) |
| S3 (multi-agent) | 95% で cascade 消える | cascade は消えた ✅。 ただし B2-H1+H2 で結果未達 | 半分当たり |
| S4 (ask_user) | 30% で直接答える | router が pre-skill clarification → IR op 非発火 | 外れ方向で的中 |
| S5 (memory) | 60% で動く | core memory 動作 ✅ | 当たり |

合計: 5 件中 3 件が「方向は当たり」。 batch 1 の 0/4 からは改善した。
ただし「当たり」 と言えるのは方向感のみで、 重要な隙間 (B2-H1 の attractor)
を見落としていた点は正直に記す。

---

## main 発見 — HIGH 3 件の要約

### B2-H1: describe まで行って止まる — F3 の亡霊、 specialist に宿る

batch 1 の F3 は「router が `invoke_skill` を呼ばない」 という attractor で、
default agent の `router_system_prompt.py` を修正して塞いだ。 S3 で
specialist 側の RouterLoop を観測してみると、 同じ attractor の変種が
専用のプロンプトに残っていた。

```
list_skills("") → list_skills("general") → describe_skill("direct_llm") → 終了
```

`invoke_skill` は呼ばれない。 specialist は `describe` を 「準備完了」 と
勘違いして静かに停止する。 F3 修正がデフォルトのプロンプトにしか当たって
いなかったことが原因で、 fix は `router_system_prompt.py` への 1 ルール追加
(`describe_skill` 後に `invoke_skill` か明示的な説明を義務付け) で `83bad83`。

### B2-H2: 失敗通知が「かしこまりました」に吸収される

F6 fix (specialist の早期空 reply 防止) は `_no_reply_marker` で specialist
が応答できなかった事実を明示する設計だった。 意図は正しい。 だが default
側がそのマーカーを受け取ったとき何をすべきかが実装されていなかった。

WAL に記録されていた実際の default reply:

```
かしこまりました。 他に何かお手伝いできることはありますでしょうか？
```

カスケードを防ぎすぎて、 失敗を伝える責任まで消えた。 OS 側での決定論的な
marker 検出 (`_is_no_reply_marker`) + LLM bypass + 直接 outbox 書き込みで
`e9216f6` で修正。 user に届くようになった出力:

```
エージェント 'specialist' から処理結果が得られませんでした
(理由: router completed without producing a text reply)。
```

### B2-H3: out-of-box の最後の関門 — permission 1 行の欠落

batch 1 の F10 は「filesystem MCP server が config に無い」 を修正し、
`with-mcp.yaml` を参照するよう doc に追記した。 S2 で実際に動かしてみると、
そのファイル自体に `permissions: mcp.filesystem: allow` が無いため
headless 環境では全 MCP 呼び出しが `permission_denied` になる。

「config ファイルをコピーするだけで動く」 という out-of-box promise が
この 1 行の欠落で成立していなかった。 `a75587d` で修正。

---

## 感覚との差 — assistant と user のズレが浮かんだ箇所

batch 1 では全 11 件を assistant 主観で重要度付けし、 A4 review で
「全件合意」 だった。 batch 1 の所感:「問題が重すぎて議論の余地がなかった」。

batch 2 は少し違う様相だった。 以下は A4 で感覚のズレが浮かびかけた箇所:

**S3 の「結果」 評価**

assistant は当初 S3 を「cascade 解消 ✅」 と書いた。 技術的には正しく、
`tool_call_deduped` × 2 / cascade retry 0 / 待ち時間 4.6s はいずれも
意図通りの挙動だった。 しかし user から見ると S3 の目標は「カレーレシピが
届く」 であって、 cascade の解消はその手段にすぎない。 技術的に正しい
中間指標を「成功」 と呼んでしまった点は assistant の認識ズレ。 B2-H1+H2
が浮かぶまで「S3 は pass」 と暗黙に思っていた。

**B2-M4 の重要度**

narrator が「完了しました」 と言って skill 出力を渡さない (B2-M4) を
MED と分類した。 assistant の観点では「skill は動いている、 最後の 1cm」。
user 観点では「2 ターン待たされる体験」 であり、 HIGH に近い MED だと
思われる。 batch 3 で重要度を見直す候補。

**B2-INFO の評価**

S4 で ask_user IR op が発火しなかったことを「観測設計の問題、 INFO」 と
分類した。 これは技術的に正確で bug ではない。 ただし user は S4 を
「ask_user が動くか見てみよう」 というユーザー体験の問いとして出していた。
設計の問題か機能の問題かという分類軸と、 「trigger できない = 動かない」
という体験軸は別物だった。 batch 3 の S4-v2 で経路を正しく踏む設計にする。

batch 1 と同様、 重大な重要度 mis-classification は今のところ無い。 ただし
「技術的に機能してる」 と「user に届いてる」 の違いが、 batch が進むにつれ
より細かいレベルで出始めている。

---

## regression net 評価の正直化

batch 2 開始直後、 findings.md の旧文面に「全 8 件 ✅」 と書いた。 これは
過大表現だった。 実際の確認経緯:

### A. 5 scenario 内で直接観測 (6 件)

F3 / F5 / F7 / F9 / F10 / F11 は 5 scenario の中で目で踏んだ。 これだけが
「直接観測」 と呼べる範囲。

### B. 間接的に確認 (2 件)

F1 (chat 起動 AttributeError) は「5 scenario 全て chat 起動した」 という
事実から「再現しなかった」 と言えるが、 AttributeError の再現条件を
直接踏んではいない。 F6 (`_no_reply_marker` 発火) は発火を確認したが
B2-H2 で別の形で失敗している。 どちらも「完全には確認できていない」。

### C. 後追い verification (3 件)

F2 / F4 / F8 は最初の 5 scenario に対応 scenario がなかった。 後から
Sonnet sub-agent を使って補完した。 このうち F4 で残バグを発見:

> `70194d5` (F4 の当初修正) 後も `_total_cost_usd` が 0 のままだった。
> 原因: `self._resolver.resolve("router")` が文字列 `"router"` を literal
> で返し、 prefix strip 条件を満たさず `estimate_cost("router", ...)` が
> None を返していた。

`d9e5fce` で `resolve("light")` に修正し、 1 turn dogfood で
`cost $0.0002 prompt=1601 completion=11` を確認して初めて F4 closed。

この流れで得た teaching: **「修正した」 と「修正が効いている」 は別**。
scenario 設計の外にある bug は事後 verification まで見えない。 batch 3 では
verification loop を scenario 設計に組み込む形を考える。

---

## process 評価 — A1-A5 と並列化の実際

### 5 step の機能評価

| Step | 評価 |
|---|---|
| A1 (scenario 作成) | 機能した。 6 軸 / 5 scenario の事前構造化で finding 漏れが減った |
| A2 (user review) | 1 往復で S4 の ask_user trigger 戦略が洗練、 scenario 過多の懸念も出た |
| A3 (実行) | 機能した。 ただし scenario 設計外の F2/F4/F8 確認が抜けた |
| A4 (user 感覚共有) | まだ来ていない (= batch 2 は A5 まで進行中) |
| A5 (分類 + fix) | HIGH 3 件は即 PR で解消、 MED/LOW は Wave B 送り |

A1-A3 は batch 1 からの改善が出ている。 A4 の user 感覚 loop がまだ完結
していない点は batch 3 前に補完すること。

### 並列化 sub-agent の効果

batch 2 では Sonnet 9 体を並列投入して scenario 実行 + 後追い verification
+ HIGH fix の 3 トラックを同時進行した。 効果と限界:

**効果**: 全 3 HIGH が当日中に fix された。 batch 1 では fix wave が
翌セッションになっていた点と比べると、 投入コストの回収速度が上がっている。
F4 residual も後追い verification の Sonnet が同日中に発見・修正した。

**限界**: 並列投入した Sonnet 間で知識の同期が取れない。 B2-H1 の fix
commit (`83bad83`) が commit message では B2-H2 ラベルになっている
(「parallel race: B2-H2 agent が B2-H1 の差分も込みで先に commit」)。
これは後から読む人間への信頼性を下げる。 commit message に両方の finding ID
を記載するルールを明示化する。

コスト面では 9 体 Sonnet はモデル price の都合で Opus 3 体より安く、
throughput は高い。 この組み合わせは継続する価値がある。

---

## 次回への持ち越し — batch 3 の設計方針

### HIGH が出た後の verification loop

batch 2 で学んだ最大の構造問題: scenario 設計段階で「このバグは修正済み」
と前提を置くと、 修正漏れの verification が自然に抜ける。 batch 3 では
「修正 commit の 1 turn e2e 確認」 を A3 に組み込む。

### ask_user e2e — S4-v2

B2-INFO で明らかになった通り、 S4 の設計が ask_user IR op を引き出す
経路を踏んでいなかった。 batch 3 向け推奨設計:

```
"read_local_files skill を使って、 このディレクトリにある report.md を読んで要約して"
```

skill 名明示 + 存在しない path の組み合わせで skill が先に起動し、
phase 内で ask_user op が発行される確率が上がる。

### narrator 品質 (B2-M4)

skill 出力が narrator に渡らない問題は「最後の 1cm」 として batch 2 では
MED 扱いだが、 user 体験としては「2 ターン待たされる」 という明確な劣化。
batch 3 で priority を再評価し、 必要なら HIGH に昇格して即 fix する。

### 残 MED/LOW の Wave B 整理

| ID | 内容 | 想定対処 |
|---|---|---|
| B2-M1 | router が skill 名を幻覚 | `list_skills` 必須ルールを router prompt に追記 |
| B2-M2 | tool_failed 後の英語 fallback | F11 の適用範囲を error 経路にも拡張 |
| B2-M3 | MCP teardown の anyio error | 機能影響なし、 長期 session での leak 調査 |
| B2-L1 | sync tool の dupe call | F5 dedupe を async 専用から sync にも拡張 |
| B2-L2 | recall 時の不要 write | memory tool description に「新情報のみ書く」 を明記 |
| B2-L3 | dogfood rig reset 手順 | `rm -rf .reyn/` を標準手順として docs に明記 |

### batch 3 の重点観測軸

1. ask_user e2e (S4-v2)
2. narrator 品質 (B2-M4 の継続観察)
3. B2-H1+H2 fix 後の multi-agent E2E (カレーレシピが届くか)
4. out-of-box experience (`with-mcp.yaml` コピーだけで headless 実行が通るか)

B2-H1+H2 の fix 確認なしに batch 3 に進まないこと。 カレーレシピが届く
確認を regression net の起点にする。

---

## まとめ

batch 2 は「regression net を e2e で踏む」 という目標に対して、 直接観測 6 +
間接 2 + 後追い 3 = 全 11 件のカバーを完了した。 その過程で F4 の残バグを
新規発見・修正し、 新 HIGH 3 件 (B2-H1/H2/H3) をすべて当日中に fix した。

一方で学んだことが二つある。 第一に、 「修正済み」 という前提は
e2e 確認なしに信頼できない。 F4 residual がその証拠で、 Tier 2 green +
修正 commit があっても実 LLM の context で bug が出ることがある。
第二に、 「技術的に機能している」 と「user に届いている」 は別の観測軸。
cascade が消えても カレーレシピが届かなければ user にとって S3 は失敗だ。

> dogfood の価値は、 test suite では見えない「届いてるか」 を問い続けること。

batch 3 で初めてカレーレシピが届いたとき、 regression net が本当の意味で
green になる。

---

*推奨読み順: [prelude](prelude.md) → [scenarios](scenarios.md) →
[findings](findings.md) → このファイル*
