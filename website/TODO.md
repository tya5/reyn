# Website TODO

## ワークフロー (= 都度の作業)

`index.html` は Claude Design 出力 (= 構造のみ、 コピーは `{{TOKEN}}`
プレースホルダ)。 本物のコピーは `copy.yaml` で管理。

### コピーを更新したい

1. `copy.yaml` を編集
2. `python website/build.py` で `dist/index.html` を再生成
3. `dist/index.html` をブラウザで確認
4. (デプロイ用ワークフローが整ったら) push で自動デプロイ

### Claude Design がレイアウトを更新した

1. 新しい `index.html` を受け取る
2. プレースホルダ規約 (`_design/claude_design_prompt.md` の
   `<copy_placeholder_convention>` セクション参照) を遵守しているか確認
3. 新しいコピー枠が増えていれば `copy.yaml` にキー追加
4. 不要キーが残っていれば削除 (build 時に warning が出る)
5. `python website/build.py` で `dist/index.html` を再生成

## 進行中

- [x] コピーを `copy.yaml` に分離 (= 再生成耐性)

## 次のステップ

- [ ] meta description / OGP タグを index.html に追加 (= 同様に
      `{{META_DESCRIPTION}}` 等のトークン化)
- [ ] ロゴ SVG 最適化（現状 206KB、nav/footer 用に小さくする）
- [ ] GitHub Pages 設定 (= `website/dist/` を deploy する workflow)
- [ ] カスタムドメイン取得・設定（reyn.dev 候補）
- [ ] LP に GitHub repo URL 表示 (= 現状 nav の "GitHub" のみ、
      hero / footer 等にもう一段 link 追加検討)
- [ ] alpha は PyPI 未公開なので install command を「git clone …」
      に差し替える検討 (`copy.yaml` の `S04_INSTALL_CMD`)

## リリース前

- [ ] GitHub リポジトリ public 化
- [ ] アナウンス（HN, X 等）
