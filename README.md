# news-watch

自治体DX・自治体の生成AI活用に関する新着Web情報を、Brave Search APIから定期収集して
Markdownの時系列ログとして蓄積するツールです。検索結果のタイトル・URL・説明文のみを保存し、
記事本文の取得やAI要約は行いません。

## セットアップ

1. [Brave Search API](https://brave.com/search/api/) でAPIキーを取得します。
2. GitHubのリポジトリで **Settings → Secrets and variables → Actions** を開き、
   Repository secret として `BRAVE_API_KEY` を登録します（値はAPIキー。コード・ファイルには書かないでください）。
3. **Actions** タブでワークフローを有効化します。

## 実行

- **自動実行**: 1日1回（日本時間 7:17 頃）。新着があった日だけ `logs/` と `data/seen.json` をcommitします。
- **手動実行**: Actions → `Collect news` → Run workflow
- **ローカル実行**（任意）: 環境変数でキーを渡します。

  ```sh
  pip install -r requirements.txt
  export BRAVE_API_KEY=...   # 各自のキーを設定
  python scripts/collect.py
  ```

- **テスト**（APIキー不要・API呼び出しなし）: `python -m unittest discover -s tests`

## 検索語の変更

`config/queries.yml` を編集します。テーマ（ログの見出し）ごとに検索語を列挙します。

```yaml
themes:
  - name: 自治体DX
    queries:
      - '"自治体DX"'   # 引用符で完全一致指定（YAMLでは全体をシングルクォートで囲む）
```

同じファイルの `exclude_domains`（ドメイン。サブドメインも対象）と `exclude_url_patterns`
（URLに含まれる文字列）で、明らかなノイズや転載URLを除外できます。
初期設定は Wikipedia・HeadTopics・docomoトピックスと、IZA／ExciteのPR TIMES転載URLです。
除外ドメインは検索語に `NOT site:` として付与され、取得後にも同じ条件で除外されます
（除外した記事は `seen.json` にも記録されません）。

検索語は合計10件までです。超えると、APIを呼ばずにエラーで停止します。
1検索語につきAPIリクエストは1回（上位10件、リトライ・ページングなし）なので、
1回の実行で最大でも10リクエストです。

## 出力

`logs/YYYY/YYYY-MM.md`（月単位）に、日付 → テーマの順で記事を追記します。
同じURLは `data/seen.json` で管理し、再度記録しません（複数の検索語でヒットした場合は
`Queries:` にまとめて1件だけ記録します）。公開日は、APIが日付を返した場合のみ `Published:` に記載します。

## GitHub Pagesで読む

収集したログを、月ごとのカード形式で閲覧できます（`index.html` / `parser.js` / `app.js` / `style.css`）。
Pagesは `logs/YYYY/YYYY-MM.md` を読み込んで表示しているだけで、データの二重管理はありません。
月の一覧 `data/months.json` は、収集時に `logs/` から自動で更新されます（手作業は不要）。

公開設定: **Settings → Pages → Build and deployment → Deploy from a branch → `main` → `/ (root)`**
公開URL例: `https://yantkys.github.io/news-watch/`

ローカル確認: リポジトリ直下で `python -m http.server` を起動し、`http://localhost:8000/` を開きます。

## 実行結果の確認

Actionsのログ末尾に、検索語数・APIリクエスト数・取得件数・新着件数・重複件数が表示されます。
APIキーは表示されません。
