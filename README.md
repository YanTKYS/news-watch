# news-watch

自治体DX・自治体の生成AI活用に関する新着Web情報を、Brave Search APIから定期収集して
Markdownの時系列ログとして蓄積するツールです。検索結果のタイトル・URL・説明文のみを保存し、
記事本文の取得は行いません。あわせて、蓄積したログからAIで振り返り記事（週刊・月刊レビュー）を生成します。

## セットアップ

1. [Brave Search API](https://brave.com/search/api/) でAPIキーを取得します。
2. GitHubのリポジトリで **Settings → Secrets and variables → Actions** を開き、
   Repository secret として `BRAVE_API_KEY` を登録します（値はAPIキー。コード・ファイルには書かないでください）。
3. （任意）関連性フィルタを使う場合は、同じ手順で Repository secret `JEV_API_KEY`（Jev APIのキー）も登録します。
4. （週刊・月刊レビュー）同じ手順で Repository secret `GEMINI_API_KEY`（[Google AI Studio](https://aistudio.google.com/) で発行したGemini APIのキー）を登録します。
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
Google Gemini API（`generateContent`、既定のモデルは `gemini-3.8-flash`）で振り返り記事を生成します
（`scripts/weekly.py` / `.github/workflows/weekly.yml`）。Brave・Jevは呼び出さず、日刊ログ・`seen.json` は読み取るだけです。
Google検索などのツールは使いません。

- **API利用量**: 1回の実行で最大1リクエスト（リトライ・自動修復・別モデルへの切り替えなし、タイムアウト180秒）。
  生成済みの週は通常実行ではAPIを呼ばずに終了し、対象週の記事が0件のときも呼びません。
- **モデルの変更**: **Settings → Secrets and variables → Actions → Variables** に Repository variable `GEMINI_MODEL`
  （例: `gemini-3.8-flash`）を登録すると、週刊・月刊ともそのモデルを使います。未設定時は `scripts/weekly.py` の `DEFAULT_MODEL` です。
  モデルが利用できない場合（HTTP 404等）は、モデル名・HTTPステータス・エラー概要をログに出して停止します。
- **入力**: 記事ごとのタイトル・テーマ・Source・Published・Retrieved・説明文（500文字まで）に `article-001` 形式のIDを付けて送ります。
  URL・検索語・月別ログ全体は送りません。最大50件で、超える場合は日付×テーマの偏りが出ないよう選び、
  選ばなかった件数をActionsログに表示します。タイトルが似ている記事（`topic_dedup.py` の判定）は
  「同一話題の候補」としてAIに伝えるだけで、記事は削除しません。
- **検証**: JSON Schema形式の構造化出力（`responseJsonSchema`）を、Pythonで再検証します（必須項目・文字数・トピック1〜5件・
  出典IDが入力に存在し重複しないこと・本文にURLを含まないこと）。候補なし・安全性によるブロック・出力上限による途中終了
  （`finishReason` が `STOP` 以外）や不正なJSONの場合は保存せず、ワークフローを失敗にします。
  思考トークンは出力上限に含まれるため、思考の上限（`THINKING_BUDGET`）と出力上限（`MAX_OUTPUT_TOKENS`）を分けて設定しています。
  出典リンクは、検証済みのIDに対応する元記事のURL（http/httpsのみ）をPython側で付けます。
- **出力**: `reviews/weekly/YYYY-Www.md`（ISO 8601の週番号。年末年始は暦年と異なる年になる場合があります）と、
  週の一覧 `data/weeks.json`。
- **手動実行**: Actions → `Weekly review` → Run workflow。`week`（例: `2026-W42`、空欄なら前週）と、
  生成済みの週を上書きする場合のみ `regenerate` を指定します。終了していない週は指定できません。
- **ローカル実行**（任意）: `GEMINI_API_KEY`（必要なら `GEMINI_MODEL`）を環境変数に設定して
  `python scripts/weekly.py [--week 2026-W42] [--force]`

## 月刊レビュー

毎月1日（日本時間 9:03 頃）に、前月の振り返り記事を生成します（`scripts/monthly.py` / `.github/workflows/monthly.yml`）。
Gemini APIの設定・モデル・API利用量（1回の実行で最大1リクエスト、リトライ・別モデルへの切り替えなし）・
出力の検証は週刊と共通です。生成済みの月は通常実行ではAPIを呼ばず、対象月の記事が0件のときも呼びません。

- **入力**: 取得日（`Retrieved`）が対象月の記事（最大150件、説明文は300文字まで）と、対象月に重なる週の週刊レビューの注目トピック。
  週刊のトピックは、出典がすべて対象月の記事に対応するものだけを渡します（月をまたぐ週の対象月外の記述は使いません）。
  週刊の概況・まとめは渡しません。週刊が無い週は日刊ログだけで補います。記事が150件を超える場合は、週刊の出典になった記事を優先します。
- **構成**: 今月の総括／主要トレンド（1〜6件。見出し・説明・自治体実務への示唆・出典）／翌月以降の注目点。
  出典リンクは、AIが返した記事IDを検証し、対象月の日刊ログにあるURLから付けます。
- **出力**: `reviews/monthly/YYYY-MM.md` と、月の一覧 `data/review-months.json`（日刊用の `data/months.json` とは別）。
- **手動実行**: Actions → `Monthly review` → Run workflow。`month`（例: `2026-10`、空欄なら前月）と、
  生成済みの月を上書きする場合のみ `regenerate` を指定します。終了していない月は指定できません。
- **ローカル実行**（任意）: `python scripts/monthly.py [--month 2026-10] [--force]`
- **実行順序**: 週刊・月刊のワークフローは同じ concurrency group で直列に実行します（1日が月曜の日も同時にpushしません）。
  月刊は実行開始時点の `main` を取得するため、先に完了した週刊も材料にできます。日刊収集とは変更ファイルが重ならず、
  push前に `git pull --rebase` で取り込みます。

## 検索インデックス

日刊タブの横断検索は、`logs/` から抽出した `data/search-index.json`（タイトル・URL・情報源・公開日・取得日・テーマ・説明文）を
ブラウザに1回だけ読み込んで絞り込みます（外部の検索サービスやAPIは使いません）。
インデックスは日刊ログの派生データで、`scripts/search_index.py` で再生成できます。同じURLは1件だけ、http/https 以外のURLの記事は含めません。
日刊収集（`collect.yml`）の後の別ステップで自動更新し、内容が変わった日だけcommitします。
このステップが失敗しても、日刊ログのcommitは通常どおり行われます。

## GitHub Pagesで読む

ヘッダーの「日刊／週刊／月刊」で表示を切り替えます（`index.html` / `parser.js` / `app.js` / `style.css`、アイコンは `favicon.svg`）。

- **日刊**: 収集したログを月ごとのカード形式で表示します。`logs/YYYY/YYYY-MM.md` を読み込むだけで、データの二重管理はありません。
  月の一覧 `data/months.json` は、収集時に `logs/` から自動で更新されます（手作業は不要）。
  検索欄にキーワードを入れると、選択中の月ではなく全期間の日刊ログから記事を探せます。
  対象はタイトル・説明文・テーマ・情報源で、部分一致・空白区切りのAND検索です（英数字の大文字小文字、全角半角は区別しません）。
  結果は取得日の新しい順に50件ずつ表示し、検索欄を空にすると通常の表示に戻ります。
- **週刊**: `data/weeks.json` の週を選んで、`reviews/weekly/YYYY-Www.md` を1カラムの記事として表示します。
  `#weekly/2026-W42` のようなURLで特定の週を直接開けます。
- **月刊**: `data/review-months.json` の月を選んで、`reviews/monthly/YYYY-MM.md` を同じ1カラムの記事として表示します。
  `#monthly/2026-10` のようなURLで特定の月を直接開けます。

公開設定: **Settings → Pages → Build and deployment → Deploy from a branch → `main` → `/ (root)`**
公開URL例: `https://yantkys.github.io/news-watch/`

ローカル確認: リポジトリ直下で `python -m http.server` を起動し、`http://localhost:8000/` を開きます。

## 実行結果の確認

Actionsのログ末尾に、検索語数・APIリクエスト数・取得件数・新着件数・重複件数が表示されます。
週刊・月刊レビューでは、対象期間・元記事数・選択件数・モデル名・APIリクエスト数・トークン数（入力・出力・思考・合計。応答に含まれる場合）・出力先が表示されます。
月刊では、参照できた週刊レビューの数と、対象月外の出典を含むため除外した週刊トピックの数も表示されます。
APIキーは表示されません。
