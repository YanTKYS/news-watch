# 運用手順

収集・レビュー生成を実行する人向けの手順。収集条件・データ形式・生成内容は[技術・データ仕様](technical.md)にまとめる。コマンドはリポジトリ直下で実行する。

## セットアップ

GitHub Actionsで運用する場合は、リポジトリの **Settings → Secrets and variables → Actions** に必要なRepository secretsを登録し、**Actions** タブでワークフローを有効にする。

| Secret | 用途 | 取得先・補足 |
| --- | --- | --- |
| `BRAVE_API_KEY` | 日刊収集に必須 | [Brave Search API](https://brave.com/search/api/) |
| `GEMINI_API_KEY` | 週刊・月刊レビュー生成に必要 | [Google AI Studio](https://aistudio.google.com/) |
| `JEV_API_KEY` | 任意の関連性フィルタ。未設定でも収集可能 | Jev（System One）用のAPIキー |

キーはコード・設定ファイル・ログに書かない。ローカル実行では必要なキーを同名の環境変数として安全に設定する。GitHub ActionsのSecretとローカルの環境変数は自動では共有されない。

週刊・月刊のモデルを変更する場合は、同じ設定画面の **Variables** にRepository variable `GEMINI_MODEL` を登録する。未設定時は `gemini-3.8-flash`（[weekly.py](../scripts/weekly.py) の `DEFAULT_MODEL`）。週刊・月刊で共通の設定で、別モデルへの自動切り替えはない。

## 定期実行と手動実行

時刻は日本時間（JST）の目安。GitHub Actionsのスケジュールは遅延することがある。APIを使う本番ワークフローのトリガーは `schedule` と `workflow_dispatch` のみで、PRでは実行しない。

| Actionsの名前 | 定期実行（JST） | Run workflowの入力 | 定義 |
| --- | --- | --- | --- |
| `Collect news` | 毎日7:17頃 | なし | [collect.yml](../.github/workflows/collect.yml) |
| `Weekly review` | 毎週月曜8:29頃 | `week`、`regenerate` | [weekly.yml](../.github/workflows/weekly.yml) |
| `Monthly review` | 毎月1日9:03頃 | `month`、`regenerate` | [monthly.yml](../.github/workflows/monthly.yml) |

手動実行は **Actions → 対象ワークフロー → Run workflow**。公開データを更新する通常運用では `main` を選ぶ。ワークフローは変更した出力をcommit・pushする。日刊は新着がなくても、拒否キャッシュや検索インデックスに差分があればcommitすることがある。

週刊・月刊は共通のconcurrency group `reviews` で直列に実行するが、週刊が必ず先に動く保証はない。月刊はジョブ実行時のブランチ先頭を取得し、その時点で存在する週刊を参照する。日刊は別グループで動き、レビュー側はpush前の `git pull --rebase` で先行更新を取り込む。

### 週刊の対象指定と欠号補完

- `week` 指定（例: `2026-W42`）：その週だけを処理する。ISO 8601の月曜〜日曜で、終了した週のみ指定可能。
- `week` 空欄・`regenerate=false`：直近4つの終了済み週から、未生成で記事がある最も古い週を1つ生成する。1回の実行で最大1週。生成済み・記事なしの週はAPIを呼ばずに飛ばし、4週より古い欠号は自動で補完しない。
- 生成済みの週は通常実行で上書きしない。上書きする場合だけ `regenerate=true` を使う。この場合、`week` 空欄なら前週を再生成し、欠号の自動選択は行わない。
- 古い欠号が繰り返し失敗して新しい週へ進めない場合は、原因を調べたうえで新しい週を `week` 指定して実行できる。

### 月刊の対象指定

`month`（例: `2026-09`）を指定するとその月、空欄なら前月を対象とする。終了した月のみ指定できる。生成済みは通常スキップし、上書きする場合だけ `regenerate=true` を指定する。記事がない月はAPIを呼ばない。週刊のような複数月の自動欠号補完はない。

## 実行結果と障害時の判断

Actionsのジョブログで成否を確認する。日刊は検索語数・APIリクエスト数・新着・重複・除外件数などを表示する。週刊・月刊は **Summary** とログに対象期間、実行区分、入力記事数、モデル、API回数、結果、エラー概要、使用トークン数を表示する。月刊では参照週刊数や対象月外の出典による除外数も確認できる。

### APIの再試行方針

| 処理 | 呼び出し・再試行 |
| --- | --- |
| Brave / Jev | リトライなし。上限・失敗時の保存方針は[技術仕様](technical.md#日刊収集)を参照 |
| Gemini（週刊・月刊共通） | 通常1回、各タイムアウト180秒。HTTP 429/500/502/503/504や一時的な接続障害に限り10秒待って1回再試行する（最大2回、合計時間予算内）。400/401/403/404、証明書検証エラー、不正な出力・途中終了では再試行しない |

タイムアウト時もサーバー側で処理・課金されている可能性がある。再実行や `regenerate` はAPI利用を増やすため、原因と保存状況を確認してから判断する。自動修復やモデルの自動フォールバックはない。

| 状況 | 確認と対応 |
| --- | --- |
| キー未設定、401/403 | 対象Secret名、キーの有効性、API利用権限を設定画面で確認。キー値をログやIssueに貼らない |
| Geminiで404 | `GEMINI_MODEL` とモデルの提供・利用可否を確認。別モデルにする場合はVariableを明示的に変更 |
| 429、5xx、タイムアウト | 上記の再試行結果とAPI側の利用枠・状態を確認し、無条件に実行を繰り返さない |
| GeminiのJSON・出典ID検証失敗、出力途中終了 | 新しいレビューは保存されない。エラー概要、対象記事、モデル設定を調べる。検証を無効にして保存しない |
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
