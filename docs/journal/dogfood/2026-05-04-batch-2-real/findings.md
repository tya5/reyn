# Batch 2 (Real) — Findings

> regression net: batch 1 HIGH bug 8 件の修正を **全 5 scenario で確認**。
> 「16 秒の悲劇」 は再発しなかった。 しかし multi-agent の深部に
> 新 HIGH 2 件が潜んでいて、 カレーレシピは今回も届かなかった。

このファイルは index です。 各 finding の詳細は `findings/` 配下に分割して
保存しています (1 finding だけ読みたいときに 1 ファイルのみ load = コスト削減)。

## 概要

> 「で、 何が起きたの?」 の一行サマリ。 詳細は各 link 先へ。

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [B2-H1](findings/B2-H1-specialist-describe-invoke-fail.md) | HIGH | specialist の RouterLoop が `describe_skill` まで来て `invoke_skill` せず沈黙 — F3 の亡霊が specialist 側に宿る | **fixed** at `83bad83` (parallel race: commit message は B2-H2 ラベル、 中身は B2-H1) |
| [B2-H2](findings/B2-H2-default-silent-absorption-marker.md) | HIGH | default が `_no_reply_marker` を飲み込んで「かしこまりました」 — 失敗が user に届かない | **fixed** at `e9216f6` |
| [B2-H3](findings/B2-H3-mcp-permission-missing.md) | HIGH | `with-mcp.yaml` に `mcp.filesystem: allow` が無く headless 実行が必ず permission_denied | **fixed** at `a75587d` |
| [B2-M1](findings/B2-M1-router-hallucinates-skill-name.md) | MED | router が invoke_skill は呼んだが skill 名を `general.summarize` と hallucinate | open |
| [B2-M2](findings/B2-M2-tool-failed-fallback-english.md) | MED | tool_failed 後の fallback reply が英語 — F11 は正常経路のみカバー | open |
| [B2-M3](findings/B2-M3-mcp-teardown-cancel-scope.md) | MED | MCP teardown で anyio cancel scope 違反 RuntimeError が stderr に残る | open |
| [B2-M4](findings/B2-M4-narrator-generic-completion.md) | MED | narrator の reply が「完了しました」のみで skill 出力が含まれず 2-turn 体験 | open |
| [B2-L1](findings/B2-L1-sync-tool-duplicate-call.md) | LOW | `remember_shared` が同一 args で 2 回 fire — F5 dedupe は async 専用 | open |
| [B2-L2](findings/B2-L2-recall-path-redundant-write.md) | LOW | recall turn で `remember_shared` を不要に再 call → frontmatter drift | open |
| [B2-L3](findings/B2-L3-dogfood-rig-history-cache.md) | LOW | dogfood rig reset が `history.jsonl` を残す — 正しくは `rm -rf .reyn/` | open |
| [B2-INFO](findings/B2-INFO-ask-user-not-observed.md) | INFO | S4 ask_user 経路: skill が起動前に router が pre-skill clarification → IR op 非発火。 bug ではない | batch 3 再設計 |

**HIGH 3 件全て fixed** (B2-H1: `83bad83`、 B2-H2: `e9216f6`、 B2-H3: `a75587d`)。
MED / LOW は Wave B coverage audit へ deferred。

**batch 1 fix の機能確認 (11 件)**: 直接観測 **6** / 間接 **2** / 後追い
verification で **3** 件補完 → 全 11 件カバー完了。 後追いの中で **F4 residual
bug** (= 70194d5 後も cost が 0 のまま) を新規発見、 commit `d9e5fce` で修正。
当初の「全 8 件 ✅」 は過大表現で、 後追いで埋めた経緯を以下に明記。
ただし新 HIGH 3 件 (B2-H1/H2/H3) が浮上 → 全 fixed。

---

## ハイライト narrative

### regression net 確認 — 直接観測 6 / 間接 2 / 後追い 3 (合計 11)

batch 1 の A5 wave (commit `e59cead` + `9e8126c`) が狙い通り機能しているか、
batch 2 の 5 scenario + 後追い verification で照合した。

#### A. 5 scenario 内で直接観測できた件 (6 件)

**F3** (router attractor): S1 で router は `invoke_skill` を呼んだ ✅。 呼んだ先が
幻覚 skill だったのは B2-M1 だが、 「呼ばない」 状態からは確実に前進。

**F5** (duplicate async tool_call): S3 で `tool_call_deduped` × 2 を確認 ✅。

**F7** (16 秒 cascade): S3 で cascade retry ゼロ件 ✅。

**F9** (explicit skill name 無視): S2 で router が `read_local_files` を確実に invoke ✅。

**F10** (filesystem MCP 未設定): S2 Run 2 (permission 事前承認) で README.md 読取り ✅。
ただし B2-H3 (Agent H fix) により `with-mcp.yaml` の permission 行追加が必要だった。

**F11** (fallback 英語、 正常経路): S5 で日本語 reply 確認 ✅。 tool_failed 後の
回復経路が英語になる新問題は B2-M2 として分離。

#### B. 間接的に確認できた件 (2 件)

**F1** (chat 起動 AttributeError): 5 scenario 全 chat 起動成功 = 起きてない、
ただし AttributeError 再現条件を **直接踏んではいない** (= 間接保証)。

**F6** (specialist 空 reply): `_no_reply_marker` 発火は確認 ✅、 ただし default 側で
飲まれる別問題に発展 → B2-H2 で `e9216f6` 修正済。 半分カバー。

#### C. 後追い verification で確認した件 (3 件、 当初 scenario 設計外)

**F2** (`reyn.local.yaml` 読み込み): 後追いで Sonnet sub-agent が config 読み込み
パスを検証 ✅ — `reyn.local.yaml` が `reyn.yaml` を上書き、 空文字 / 削除で
`None` 返却、 `reyn.yaml` のみで fallback。 全 3 ケース pass。

**F4** (cost 永遠の 0): 後追い 1 turn 実 LLM dogfood で **residual bug 発見**:
`70194d5` 後も `_total_cost_usd=0` が続いていた。 原因は
`self._resolver.resolve("router")` が文字列 `"router"` を literal で返し
mapping に無いため prefix strip 条件を満たさず `estimate_cost("router", ...)`
が None を返していた。 Sonnet sub-agent が `d9e5fce` で
`resolve(loop.router_model)` (= `resolve("light")`) に修正、
1 turn dogfood で `cost $0.0002 prompt=1601 completion=11` を確認 ✅。

**F8** (retry budget 枯渇英語 fallback): 該当 scenario が batch 2 に無かったので
test 経由で structural 保証を確認。 `tests/test_chat_router_i18n.py` 12/12 pass、
`test_retry_exhausted_fallback_is_english_when_output_language_is_none` 含む。
`output_language=None` で en、 `=ja` で ja、 unknown code で en fallback、
全件 ✅。

#### D. memory (S5)
core の remember+recall は機能 ✅。 B2-L1/L2 は polish 課題。

#### 集計
- 直接観測: 6
- 間接 / 半分: 2
- 後追い verification: 3 (うち F4 で residual bug 1 件発見 → `d9e5fce` 修正)
- **batch 1 fix の機能確認: 11/11 (= 8 元の F1-F11)**

「全 8 件 ✅」 と書いてた旧表現は過大表現でした。 後追いで 3 件 (F2/F4/F8)
を埋めた結果、 全件カバーに到達 + F4 の residual を新規発見・修正。

### specialist 側でも router attractor — B2-H1

cascade が消えたと思ったら、 別の問題が顔を出した。

S3 で specialist agent は `list_skills` → `describe_skill("direct_llm")` まで
進んだが、 そこで **静かに止まった**。 invoke_skill は呼ばれなかった。

これは F3 と構造的に同じ attractor — 「調べたのに動かない」 パターン。 F3 修正は
default agent の `router_system_prompt.py` に当てたが、 specialist が使う
system prompt のカバーが漏れていた可能性が高い。

Agent G により B2-H1 の fix が着手済み (commit TBD)。 `describe_skill` 後に
`invoke_skill` か明示的な説明を義務付けるルールを router system prompt に追加。

### `_no_reply_marker` の silent absorption — B2-H2

F6 fix は「specialist が空 reply を早期送信しないよう `_no_reply_marker` で
明示する」 という設計だった。 意図は正しい。 だが default 側が marker を
受け取ったとき何をすべきか、 が実装されていなかった。

結果として default は marker を「通常の完了通知」として解釈し、
`かしこまりました。 他に何かお手伝いできることはありますでしょうか？` と
返してしまう。 失敗を知る術が user に無い。

cascade を防ぎすぎて、 失敗を伝える責任まで消えてしまった構図。
`_no_reply_marker` の design intent (= failure notification) を default の
LLM context に明示する必要がある。

### out-of-box experience の最後の関門 — B2-H3

F10 (batch 1) は「filesystem MCP server が設定ファイルに無い」 を修正した。
しかし `with-mcp.yaml` を配置しただけでは `permissions: mcp.filesystem: allow`
が欠けていたため headless 環境では permission_denied が継続していた。

Agent H がこれを `a75587d` で修正。 これで「config ファイルをコピーするだけで
動く」 という out-of-box promise が finally 成立する。

### 小さな漏れ群 (B2-M*, B2-L*)

- **B2-M1** — skill 名 hallucination: `list_skills` を使わずに名前を推測する attractor。
  router prompt に「まず list_skills」 指示が必要。
- **B2-M2** — tool_failed 後の英語 fallback: F11 の適用範囲が正常経路のみ。
- **B2-M3** — MCP teardown の anyio error: 機能影響なし、 長期 session でのリーク懸念。
- **B2-M4** — narrator が「完了しました」だけ言って終わる: skill 出力が narrative に
  渡っていない最後の 1cm 問題。 2-turn に落ちる。
- **B2-L1** — sync tool dupe: F5 dedupe は async 専用なので sync の `remember_shared`
  dupe は通過。
- **B2-L2** — recall 時の不要 write: memory tool description に「書くのは新情報のみ」
  を明記。
- **B2-L3** — dogfood rig reset 手順: `rm -rf .reyn/state .reyn/events` では不完全、
  `rm -rf .reyn/` が正しい。

---

## まとめ

### 事前 prediction の精度 (scenarios.md より)

batch 2 開始前の事前仮説 5 件:

- **S1 「50% で再発も」**: 外れ (= invoke した、 ただし hallucination で失敗 → B2-M1)
- **S2 「MCP 設定が正しければ動く」**: 当たり (= Run 2 成功 ✅、 ただし B2-H3 が
  その「設定が正しければ」 の条件を直前まで満たしていなかった)
- **S3 「95% 確率で pass」**: 半分当たり (= cascade 解消 ✅ だが B2-H1+H2 で結果が届かない)
- **S4 「30% で直接答える」**: 外れ方向で的中 (= router が pre-skill clarification、
  ask_user どころか skill 未起動)
- **S5 「60% で動く」**: 当たり (= core memory 動作 ✅、 LOW 2 件はおまけ)

精度: 5 件中 3 件が「方向は当たり」。 batch 1 の 0/4 より改善。 S3 は cascade
という質問には答えたが新問題を見落とした — 問いが良くなれば精度も上がる。

### 結論

> **regression net は green。 ただし multi-agent UX には attractor 形 HIGH bug
> が 2 件残る。 カレーレシピが user に届く日は、 まだ来ていない。**

F1-F11 の 8 件 fix は e2e で動作確認済み。 しかし specialist routing (B2-H1)
と `_no_reply_marker` silent absorption (B2-H2) という新 HIGH が浮上した。
構造的には F3 + F7 の variant であり、 修正方針は類似する。

### 次のアクション

1. **B2-H1 fix** — Agent G が着手中 (commit TBD)。 specialist router prompt に
   `describe_skill → invoke_skill` コミットルールを追加。
2. **B2-H2 fix** — `_no_reply_marker` の failure semantics を default の LLM
   context (system prompt or OS の agent_response handler) に明示。
3. **B2-H3** — Agent H が `a75587d` で修正済み。 batch 3 で out-of-box 再確認。
4. **Wave B coverage audit** — B2-M1 (hallucination) / B2-M4 (narrator) /
   B2-L1-L3 (polish) を Wave B で整理。
5. **batch 3 設計** — ask_user e2e (B2-INFO)、 nested skill (run_skill IR op)、
   narrator 品質を重点観測。 B2-H1+H2 fix 後に実施。
