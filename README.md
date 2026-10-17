# news-watch

自治体DX・自治体の生成AI活用に関する新着Web情報を、Brave Search APIから定期収集して
Markdownの時系列ログとして蓄積するツールです。検索結果のタイトル・URL・説明文のみを保存し、
記事本文の取得は行いません。あわせて、1週間分のログからAIで振り返り記事（週刊レビュー）を生成します。

## セットアップ

1. [Brave Search API](https://brave.com/search/api/) でAPIキーを取得します。
2. GitHubのリポジトリで **Settings → Secrets and variables → Actions** を開き、
   Repository secret として `BRAVE_API_KEY` を登録します（値はAPIキー。コード・ファイルには書かないでください）。
3. （任意）関連性フィルタを使う場合は、同じ手順で Repository secret `JEV_API_KEY`（Jev APIのキー）も登録します。
4. （週刊レビュー）同じ手順で Repository secret `SARASHINA_API_KEY`（Sarashina APIのキー）を登録します。
5. **Actions** タブでワークフローを有効化します。

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

## Jevによる関連性フィルタ

Brave Searchで取得した新規候補のうち、除外設定と重複排除を通過したURLだけをJev（System One）で
「日本の自治体DX・生成AI活用に関係する記事か」判定します。Brave検索結果のタイトル・説明文・検索語だけを送り、
記事本文は取得しません。
明確に非関連（Yes確率 0.30 未満）と判断した記事だけを除外し、除外した記事は `seen.json` にも記録しません。
`JEV_API_KEY` 未設定・API障害・利用上限到達時は記事を除外せず保存し、収集を継続します（リトライなし）。
Jevへの問い合わせは1回の実行で最大20件です。非関連と判定したURLは `data/jev-rejected.json` に7日間記録し、
その間は再判定しません（`seen.json` には記録しません）。

必要なSecret: `BRAVE_API_KEY`（必須）、`JEV_API_KEY`（任意）

## 同一話題の重複候補（検証中）

URLが異なっていても同じ発表を扱っている記事（転載・再配信）を、タイトルの類似度だけで検出し、
Actionsログに `Topic duplicate candidates` / `Topic duplicate pairs` として表示します（`scripts/topic_dedup.py`）。
新規保存候補を、直近7日間（取得日 Retrieved 基準）の `logs/` の記事と、新規候補同士で比較します。
**現在は観測のみで、記事は従来どおりすべて保存します**（`seen.json`・ログ形式・API呼び出し数は変わりません）。
閾値は `topic_dedup.py` の `TOPIC_*` 定数で調整できます。見出しが書き換えられた記事など、曖昧な組み合わせは候補にしません。

## 出力

`logs/YYYY/YYYY-MM.md`（月単位）に、日付 → テーマの順で記事を追記します。
同じURLは `data/seen.json` で管理し、再度記録しません（複数の検索語でヒットした場合は
`Queries:` にまとめて1件だけ記録します）。公開日は、APIが日付を返した場合のみ `Published:` に記載します。

## 週刊レビュー

毎週月曜（日本時間 8:29 頃）に、前週の月曜〜日曜に取得した記事（`Retrieved` 基準。公開日では絞り込みません）から、
Sarashina API（`sarashina3-mini`）で振り返り記事を生成します（`scripts/weekly.py` / `.github/workflows/weekly.yml`）。
Brave・Jevは呼び出さず、日刊ログ・`seen.json` は読み取るだけです。

- **API利用量**: 1回の実行で最大1リクエスト（リトライ・自動修復なし、タイムアウト180秒）。
  生成済みの週は通常実行ではAPIを呼ばずに終了し、対象週の記事が0件のときも呼びません。
- **入力**: 記事ごとのタイトル・テーマ・Source・Published・Retrieved・説明文（500文字まで）に `article-001` 形式のIDを付けて送ります。
  URL・検索語・月別ログ全体は送りません。最大50件で、超える場合は日付×テーマの偏りが出ないよう選び、
  選ばなかった件数をActionsログに表示します。タイトルが似ている記事（`topic_dedup.py` の判定）は
  「同一話題の候補」としてAIに伝えるだけで、記事は削除しません。
- **検証**: JSON Schema形式の構造化出力を、Pythonで再検証します（必須項目・文字数・トピック1〜5件・
  出典IDが入力に存在し重複しないこと・本文にURLを含まないこと）。不正な場合は保存せず、ワークフローを失敗にします。
  出典リンクは、検証済みのIDに対応する元記事のURL（http/httpsのみ）をPython側で付けます。
- **出力**: `weekly/YYYY/YYYY-Www.md`（ISO 8601の週番号。年末年始は暦年と異なる年になる場合があります）と、
  週の一覧 `data/weeks.json`。
- **手動実行**: Actions → `Weekly review` → Run workflow。`week`（例: `2026-W42`、空欄なら前週）と、
  生成済みの週を上書きする場合のみ `regenerate` を指定します。終了していない週は指定できません。
- **ローカル実行**（任意）: `SARASHINA_API_KEY` を環境変数に設定して `python scripts/weekly.py [--week 2026-W42] [--force]`

## GitHub Pagesで読む

ヘッダーの「日刊／週刊」で表示を切り替えます（`index.html` / `parser.js` / `app.js` / `style.css`、アイコンは `favicon.svg`）。

- **日刊**: 収集したログを月ごとのカード形式で表示します。`logs/YYYY/YYYY-MM.md` を読み込むだけで、データの二重管理はありません。
  月の一覧 `data/months.json` は、収集時に `logs/` から自動で更新されます（手作業は不要）。
- **週刊**: `data/weeks.json` の週を選んで、`weekly/YYYY/YYYY-Www.md` を1カラムの記事として表示します。
  `#weekly/2026-W42` のようなURLで特定の週を直接開けます。

公開設定: **Settings → Pages → Build and deployment → Deploy from a branch → `main` → `/ (root)`**
公開URL例: `https://yantkys.github.io/news-watch/`

ローカル確認: リポジトリ直下で `python -m http.server` を起動し、`http://localhost:8000/` を開きます。

## 実行結果の確認

Actionsのログ末尾に、検索語数・APIリクエスト数・取得件数・新着件数・重複件数が表示されます。
週刊レビューでは、対象週・元記事数・選択件数・モデル名・APIリクエスト数・トークン数（応答に含まれる場合）・出力先が表示されます。
APIキーは表示されません。
