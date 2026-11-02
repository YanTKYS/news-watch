# 運用手順

収集・レビュー生成を実行する人向けの手順。収集条件・データ形式・生成内容は[技術・データ仕様](technical.md)にまとめる。コマンドはリポジトリ直下で実行する。

## セットアップ

GitHub Actionsで運用する場合は、リポジトリの **Settings → Secrets and variables → Actions** に必要なRepository secretsを登録し、**Actions** タブでワークフローを有効にする。

| Secret | 用途 | 取得先・補足 |
| --- | --- | --- |
| `BRAVE_API_KEY` | 日刊収集に必須 | [Brave Search API](https://brave.com/search/api/) |
| `PLAMO_API_KEY` | 週刊レビュー生成に必要 | [PLaMo API](https://plamo.preferredai.jp/api) |
| `GEMINI_API_KEY` | 月刊レビュー生成に必要 | [Google AI Studio](https://aistudio.google.com/) |
| `JEV_API_KEY` | 任意の関連性フィルタ。未設定でも収集可能 | Jev（System One）用のAPIキー |

キーはコード・設定ファイル・ログに書かない。ローカル実行では必要なキーを同名の環境変数として安全に設定する。GitHub ActionsのSecretとローカルの環境変数は自動では共有されない。

週刊は `plamo-3.1-prime` 固定で、`GEMINI_API_KEY` / `GEMINI_MODEL` は参照しない。月刊は従来どおりGeminiを使い、同じ設定画面の **Variables** のRepository variable `GEMINI_MODEL` を参照する。未設定時は `gemini-3.8-flash`（[weekly.py](../scripts/weekly.py) に残す月刊用 `DEFAULT_MODEL`）。週刊の切替時に既存のRepository variableは変更しない。どちらも別モデルへの自動切り替えはない。

## 定期実行と手動実行

時刻は日本時間（JST）の目安。GitHub Actionsのスケジュールは遅延することがある。APIを使う本番ワークフローのトリガーは `schedule` と `workflow_dispatch` のみで、PRでは実行しない。

| Actionsの名前 | 定期実行（JST） | Run workflowの入力 | 定義 |
| --- | --- | --- | --- |
| `Collect news` | 毎日7:17頃 | なし | [collect.yml](../.github/workflows/collect.yml) |
| `Weekly review` | 毎週月曜8:29頃 | `week`、`regenerate` | [weekly.yml](../.github/workflows/weekly.yml) |
| `Monthly review` | 毎月1〜3日9:23頃 | `month`、`regenerate` | [monthly.yml](../.github/workflows/monthly.yml) |

手動実行は **Actions → 対象ワークフロー → Run workflow**。公開データを更新する通常運用では `main` を選ぶ。ワークフローは変更した出力をcommit・pushする。日刊は新着がなくても、拒否キャッシュや検索インデックスに差分があればcommitすることがある。

週刊・月刊は共通のconcurrency group `reviews` で直列に実行するが、週刊が必ず先に動く保証はない。月刊はジョブ実行時のブランチ先頭を取得し、その時点で存在する週刊を参照する。日刊は別グループで動き、レビュー側はpush前の `git pull --rebase` で先行更新を取り込む。

### 週刊の対象指定と欠号補完

- `week` 指定（例: `2026-W42`）：その週だけを処理する。ISO 8601の月曜〜日曜で、終了した週のみ指定可能。
- `week` 空欄・`regenerate=false`：直近4つの終了済み週から、未生成で記事がある最も古い週を1つ生成する。1回の実行で最大1週。生成済み・記事なしの週はAPIを呼ばずに飛ばし、4週より古い欠号は自動で補完しない。
- 生成済みの週は通常実行で上書きしない。上書きする場合だけ `regenerate=true` を使う。この場合、`week` 空欄なら前週を再生成し、欠号の自動選択は行わない。
- 古い欠号が繰り返し失敗して新しい週へ進めない場合は、原因を調べたうえで新しい週を `week` 指定して実行できる。

#### PLaMo切替後のW43・W44復旧と定期実行確認

コードのPRでは実API起動・公開・Variable更新を行わない。マージ後にユーザーが以下を明示操作する。

1. `PLAMO_API_KEY` のRepository Secretと利用枠を確認する。既存の `GEMINI_MODEL` は月刊用として変更しない。
2. **Weekly review → Run workflow → main** で `week=2026-W43`、`regenerate=false` を指定し、1週だけ生成する。Summaryのモデルが `plamo-3.1-prime`、正常時API1回（限定再試行を含め最大2回）であることを確認する。
3. `reviews/weekly/2026-W43.md` と `data/weeks.json` のcommit/push、公開サイトの `#weekly/2026-W43` 表示、内容と出典の整合を確認する。成功後、同じ手順で `week=2026-W44` を**別の手動実行**で生成し、保存・索引・Pagesの `#weekly/2026-W44` を確認する。既に生成済みならAPI0回でスキップし、必要性を確認せず `regenerate` を有効にしない。
4. W43のPoC候補は構造検証に成功しているが、出典と説明の整合には確認事項がある。**Artifactのcandidateを無審査で公開へコピーしない。通常の手動生成で作り直す手順を優先する。** 新しい生成結果も構造検証だけで事実性を保証しないため、内容を確認する。
5. **2026-11-09（月）JST 08:29頃の定期実行**を確認する。W43・W44の復旧後は対象2026-W45を想定するが、直近4週に他の欠号があれば従来どおり最古の未生成週が先になる。1回のActionsで最大1週の仕様は維持する。

再失敗した場合だけHTTP・試行数・エラー概要を診断し、無条件の追加実行やGeminiへの自動フォールバックは行わない。月刊の設定・生成は別系統のまま維持する。

### 月刊の対象指定

`month`（例: `2026-09`）を指定するとその月、空欄なら前月を対象とする。終了した月のみ指定できる。生成済みは通常スキップし、上書きする場合だけ `regenerate=true` を指定する。記事がない月はAPIを呼ばない。週刊のような複数月の自動欠号補完はない。

定期起動はUTCの `23 0 1-3 * *`（毎月1・2・3日JST 09:23）。どの日も対象は実行時点の前月で、1日の起動が欠落・失敗した場合に2・3日にも生成の機会を設ける。通常は最初の成功で前月号を保存し、翌日以降は**API呼び出し0回・レビュー変更なし**でスキップする。スキップ判定は、共通concurrencyによる待機後に取得した最新のブランチ状態で行う。定期処理は強制再生成せず、`regenerate=true` は手動入力に限る。

各実行のGemini APIは既存の最大2回を維持するが、3日とも未生成のままAPI障害が続けば、**定期起動3回で最大6回の送信試行**になり得る（手動実行・ジョブの再実行は別枠）。タイムアウト後のサーバー側処理・課金も考慮し、追加の無条件再実行は避ける。4日以降に自動で繰り返したり、過去の欠号へ遡ったりはしない。

GitHubの[`schedule`の仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)では、高負荷時の遅延・欠落があり得る。複数日に機会を増やしても起動は完全保証できない。3日を過ぎても未生成なら、Actionsでワークフローの有効状態、デフォルトブランチ上の定義、リポジトリの活動状況、scheduled actorの条件と実行履歴を確認し、必要に応じて `month` を指定して手動実行する。公開リポジトリは60日間活動がないと定期実行が自動無効化され得る。actorの条件はアカウントやEnterprise設定にも依存し、手動実行の成功だけでは定期実行の条件を証明できない。

週刊が不足していても月刊は日刊から生成できる。ログの `Weekly reviews: 使用可能号数 of 対象号数 (missing: ...; daily logs are used instead)` で不足号を、`Weekly topics used` と生成物末尾の「参照した週刊レビュー」で実際に入力へ利用した件数を確認する。使用可能号数と利用件数は異なり、週刊の欠号だけで月刊の失敗とは判断しない。欠号補完・内容の改善はこの定期起動対策とは分けて扱う。

## 実行結果と障害時の判断

Actionsのジョブログで成否を確認する。日刊は検索語数・APIリクエスト数・新着・重複・除外件数などを表示する。週刊・月刊は **Summary** とログに対象期間、実行区分、入力記事数、モデル、API回数、結果、エラー概要、使用トークン数を表示する。月刊では参照週刊数や対象月外の出典による除外数も確認できる。

### APIの再試行方針

| 処理 | 呼び出し・再試行 |
| --- | --- |
| Brave / Jev | リトライなし。上限・失敗時の保存方針は[技術仕様](technical.md#日刊収集)を参照 |
| PLaMo（週刊）/ Gemini（月刊） | 通常1回、各タイムアウト180秒。HTTP 429/500/502/503/504や一時的な接続障害に限り10秒待って1回再試行する（最大2回、合計時間予算内）。400/401/403/404、証明書検証エラー、不正な出力・途中終了では再試行しない |

タイムアウト時もサーバー側で処理・課金されている可能性がある。再実行や `regenerate` はAPI利用を増やすため、原因と保存状況を確認してから判断する。自動修復やモデルの自動フォールバックはない。

| 状況 | 確認と対応 |
| --- | --- |
| キー未設定、401/403 | 対象Secret名、キーの有効性、API利用権限を設定画面で確認。キー値をログやIssueに貼らない |
| Geminiで404 | `GEMINI_MODEL` とモデルの提供・利用可否を確認。別モデルにする場合はVariableを明示的に変更 |
| PLaMoで429・503等 | HTTPコード（429は `rate_limited`）をSummary/ログで確認。エラー本文は取得・転記しない。利用枠と提供元の状態を調べ、別モデルへ自動切替しない |
| 429、5xx、タイムアウト | 上記の再試行結果とAPI側の利用枠・状態を確認し、無条件に実行を繰り返さない |
| JSON・出典ID検証失敗、出力途中終了 | 新しいレビューは保存されない。エラー概要、対象記事、モデル設定を調べる。週刊のモデル不一致も保存せず、明示再生成の失敗時は既存のレビューを保持する。検証を無効にして保存しない |
| Braveの検索失敗 | 一部の検索だけ失敗した場合は残りの収集を継続する。全検索失敗は実行失敗。新着0件と失敗を区別してログを確認 |
| Jevの障害・未設定・上限到達 | 候補を除外せず保存して収集を継続する。Jev関連の警告と件数を確認 |
| 検索インデックス更新失敗 | 日刊のcommitは継続するため、閲覧と検索に差が出ることがある。下記の再生成手順で差分を確認 |
| commit/push失敗 | 生成と公開を区別し、ジョブログとリポジトリ上の保存状況を確認。APIを再実行する前に生成済み出力を確認 |

## ローカルの準備と確認

Python 3.12を使う。Node.jsも用意すると、JavaScriptパーサー・検索のテストを含めて実行できる（Node不在では該当テストがスキップされる）。

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

テストはAPIキー不要・API呼び出しなし。以降のコマンドは仮想環境を有効にしたシェルで実行する。APIキーを環境変数で設定したうえで、必要な処理だけを実行する。

```sh
python scripts/collect.py
python scripts/weekly.py --week 2026-W42
python scripts/monthly.py --month 2026-09
```

これはそれぞれ独立した実行例で、一括実行の手順ではない。週刊の `--week`、月刊の `--month` は省略可能で、対象選択は上記のActionsと同じ。再生成には `--force` を使う。収集は日刊ログ等を、レビュー生成は各レビューと一覧JSONを変更する。

### 検索インデックスの再生成

```sh
python scripts/search_index.py
git diff -- data/search-index.json
```

外部APIは使わず、既存ログから派生ファイルを再構築する。内容が同じなら書き込まない。差分を確認して必要な変更だけをcommit・公開する。日刊ログや `seen.json` の削除・初期化は不要。

## GitHub Pagesとローカル配信

公開設定は **Settings → Pages → Build and deployment → Deploy from a branch → `main` → `/ (root)`**。公開先は [自治体DX Watch](https://yantkys.github.io/news-watch/)。配信にビルド工程やAPIキーは不要。

ローカルでは以下を起動し、ブラウザで `http://localhost:8000/` を開く。

```sh
python -m http.server 8000
```

日刊・週刊・月刊の切り替えと日刊検索を確認し、終了時はCtrl+Cで止める。レビューの直接リンクは `#weekly/2026-W42`、`#monthly/2026-09` の形式。

PLaMoの手動実行・Artifact・実測結果は[実験用README](../experiments/plamo-w42/README.md)と[比較評価](../experiments/plamo-w42/evaluation.md)を正本とし、本番の運用と分ける。
