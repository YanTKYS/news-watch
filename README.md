# news-watch

自治体DX・自治体の生成AI活用に関するニュースを集め、日刊ログとAIによる週刊・月刊レビューで振り返るツールです。検索結果のタイトル・URL・説明文を保存し、記事本文は取得しません。

**公開サイト：[自治体DX Watch](https://yantkys.github.io/news-watch/)**

## 閲覧する

- **日刊**：月・日付ごとの新着記事を確認できます。
- **週刊・月刊**：動向のまとめ、自治体実務への示唆、出典を読めます。
- **検索**：日刊の全期間をキーワードで横断検索できます。

閲覧にAPIキーは不要です。AIレビューは元記事の出典とあわせて確認してください。

## 収集・生成を始める

1. リポジトリの **Settings → Secrets and variables → Actions** で、日刊収集用の `BRAVE_API_KEY`、週刊・月刊生成用の `GEMINI_API_KEY` を登録します。関連性フィルタ用の `JEV_API_KEY` は任意です。
2. **Actions** を有効にし、`Collect news` / `Weekly review` / `Monthly review` の **Run workflow** から必要な処理を実行します。

APIキーの取得先、定期実行、対象期間・再生成の指定、公開設定は[運用手順](docs/operations.md)を参照してください。

## 開発・テスト

Python 3.12とNode.jsを用意し、リポジトリ直下で実行します。テストはAPIキー不要・外部API呼び出しなしです。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

仮想環境やローカル配信の手順も[運用手順](docs/operations.md)にあります。

## ドキュメント

| 文書 | 内容 |
| --- | --- |
| [運用手順](docs/operations.md) | セットアップ、Actions、欠号補完・再生成、障害対応、Pages・ローカル確認 |
| [技術・データ仕様](docs/technical.md) | 検索語・除外・関連性判定、保存形式、検索、レビュー生成と検証 |
| [改善・機能追加案](docs/feature-ideas.md) | 将来の提案。実装済み機能の仕様書とは区別 |
| [PLaMo W42比較PoC](experiments/plamo-w42/README.md) | 独立した実験の手順と[比較評価](experiments/plamo-w42/evaluation.md)。本番生成はGeminiを使用 |
