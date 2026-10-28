# PLaMo W42比較PoC（Issue #19）

対象は `plamo-3.1-prime` と2026-W42（10月12〜18日）の15記事だけ。本番切替・Geminiフォールバック・月刊・W40の検証は行わない。本番スクリプトと生成物は変更しない。

## 実行

リポジトリ直下から、APIなしの準備・入力確認を行う。

```sh
.venv/bin/python scripts/plamo_weekly_poc.py
.venv/bin/python -m unittest discover -s tests -v
```

APIキーが設定済みでも、通常実行はAPIを呼ばない。W42の記事集合・順序・プロンプト・スキーマのハッシュと記事ID/URL対応を記録する。入力ペイロードの期待ハッシュはスクリプト内で固定し、15件でも内容が変われば停止する。これはチェックアウト上の比較基準の固定であり、過去のGeminiリクエスト自体の保存記録ではない。

実API実行の前提は、公式仕様の確認、接続先 `api.platform.preferredai.jp` の許可、環境変数 `PLAMO_API_KEY` の安全な設定。キーをコード・ファイル・チャットに書かない。キーがなければAPIを呼ばず終了コード2で停止する。

```sh
.venv/bin/python scripts/plamo_weekly_poc.py --execute --spec-confirmed
```

1回の実行で最大1回、タイムアウト180秒。リトライ・リダイレクト追従・モデル切替・Web検索・元記事取得は行わない。タイムアウトでもサーバー側の処理・課金はあり得るため、失敗したコマンドを自動で繰り返さない。キーがあっても `--spec-confirmed` なしでは送信前に停止する。このフラグは実行者が下記の仕様照合を完了したことの申告であり、サービス互換性を自動判定するものではない。

## 手動ActionsとArtifact回収

`.github/workflows/plamo-w42-poc.yml` は `workflow_dispatch` だけを許可する。schedule / push / pull_request からは動作しない。既定値はAPIなしの準備実行で、`execute=true` と `spec_confirmed=true` を両方指定した場合だけ1回送信する。APIキーは実行ステップでRepository Secret `PLAMO_API_KEY` から注入する。クラウド環境設定のキーとは別の設定なので、登録済みとは仮定しない。

Actionsの「PLaMo W42 PoC」から対象ブランチを選び、まず `execute=false` で入力とArtifactを確認する。仕様確認とSecret設定後に実行する場合のCLI例:

```sh
gh workflow run plamo-w42-poc.yml --repo YanTKYS/news-watch \
  --ref feat/plamo-w42-poc -f execute=true -f spec_confirmed=true
```

このコマンドは有料APIを呼ぶ手動操作であり、今回の修正作業では実行していない。各runに一意の名前を使い、APIステップの失敗時も `always()` で `input-manifest.json`、`metrics.json`、`report.md` と成功時のみ存在する `candidate.md` をArtifactにする。キー未設定等の送信前失敗でファイルがなければアップロードは警告のみとなり、ジョブは失敗のまま残る。生応答やエラー本文、ディレクトリ全体はアップロードしない。保持期間は14日。ワークフローは読み取り権限のみで、生成物のcommit/pushは行わない。

ArtifactはActionsの実行画面または以下で取得し、APIを再実行せず比較する。

```sh
gh run download RUN_ID --repo YanTKYS/news-watch \
  --name plamo-w42-RUN_ID-RUN_ATTEMPT --dir /tmp/plamo-w42-results
```

### PRマージ前の実API検証

**新規の `workflow_dispatch` ワークフローは、デフォルトブランチに登録されるまで手動起動できない。** `--ref` を指定するだけではこの制約は解消しない。今回はワークフローをmainへ直接配置したり、PR全体を検証目的でマージしたりしない。

優先する経路は、PLaMoへ接続できる信頼済みローカル環境でこのPRブランチをチェックアウトし、公式仕様を照合後、環境変数でキーを渡して上記の `--execute --spec-confirmed` を1回だけ実行すること。結果の4ファイルを確認し、Git管理対象外のrunから必要な測定値と評価だけを `evaluation.md` に転記する。

ローカル実行ができずActionsを使う場合は、この手動専用ワークフローだけを先に配置する別PRをレビュー・マージしてから、PoCコードのあるブランチを `--ref` に指定する。先行配置自体はAPIを呼ばない。先行PRの作成・マージと実API起動はまだ行っていない。どちらもできない間は本PRをDraft・検証未完了のまま保つ。

## 出力

`experiments/plamo-w42/runs/<UTC時刻>/` に保存する。`--run-name NAME` で名前を指定できるが、既存ディレクトリの上書きは拒否する。生成物はGit管理対象外。

- `input-manifest.json`: 記事IDと元URL、入力・プロンプト・スキーマのハッシュ、スキーマ調整差分。
- `metrics.json`: モデル、試行回数、HTTPステータス、所要秒、構造検証結果、使用トークン数。接続前エラーも送信試行1回として数えるため、サーバー受信回数を保証する値ではない。
- `report.md`: 実行概要とGemini基準値。記事品質の比較は自動採点しない。
- `candidate.md`: 応答と既存Python検証に合格した場合のみ。出典URLは入力IDからPythonで付与する。

入力・キーの事前検査で失敗した場合は新規runを作成しない。出力準備の途中で失敗した場合は不完全なディレクトリが残ることがあるが、APIは呼ばない。実行後の保存自体が失敗した場合は標準エラーを確認し、APIを再実行せず保存先を調査する。HTTPエラー本文、応答全文、リクエスト本文、認証ヘッダー、キーはログ・指標に出力しない。失敗箇所は `metrics.json` の固定値 `stage` で区別する。

`choices` 1件、`finish_reason=stop`、モデル名一致、テキスト本文を必須とし、拒否・ツール呼び出し・途中終了・サイズ超過・不正JSON・余分なプロパティ・未知/重複出典IDを拒否する。項目別文字数などは既存の `weekly.validate_review` を再利用する。モデルが未知の別名を返す場合も、自動で許容せず仕様と実測を確認する。

## 仕様差分と未検証点

Issue記載のChat Completions形式を採用し、既存の `weekly.response_schema` を `response_format.json_schema.schema` にそのまま渡す。`name=weekly_review`、`strict=true`、`max_tokens=16384`、`stream=false`。スキーマから制約を削除したり、失敗後に緩めて再送したりしない。

公式資料: <https://docs.plamo.preferredai.jp/ja/api>

2026-10-28時点のこの環境では、公式資料へのCONNECTがプロキシの403で拒否された。レビュー対応時にも適用済み接続許可は `api.github.com` のみでPLaMoキーはなく、仕様との照合は未完了。以下は実行前に公式API仕様・利用例で確認する一覧であり、確認済みサポート一覧ではない。

| 項目 | PoCでの送信・受入条件 | 公式照合 | Python側の検証・制限 |
| --- | --- | --- | --- |
| endpoint / 認証 | `/v1/chat/completions` / Bearer | Issue記載を採用、公式未確認 | 接続先固定・転送禁止 |
| モデル | `plamo-3.1-prime` | 提供状況・返却model表記は未確認 | 応答のmodelが完全一致しなければ不合格 |
| 構造化出力 | `type=json_schema` / `strict=true` | オプションとサポート範囲は未確認 | 不正JSON・余分/欠落プロパティを拒否 |
| `type` / `required` / `additionalProperties=false` | 既存スキーマのまま | 未確認 | 型・必須項目・正確なキー集合を検証 |
| `enum` | 入力記事IDのみ | 未確認 | 既知IDのみ許可、重複IDも拒否 |
| `minItems` / `maxItems` | topics 1〜5、出典1〜8 | 未確認 | 同じ配列長制約を検証 |
| `max_tokens` | 16384 | パラメータ名・上限は未確認 | 途中終了を拒否。トークン上限自体の代替検証ではない |
| 応答 | choices 1件、`finish_reason=stop` | モデル別表記は未確認 | 不完全出力、拒否、ツール呼出を拒否 |
| usage | prompt/completion/total_tokens | 定義・価格は未確認 | 非負整数のみ記録、モデル間単純比較はしない |

現時点で削除・緩和したスキーマ制約はなく、manifestの `schema_adjustments` は空。上記のPython検証は二重検査であり、API側が未知の制約を受け付ける根拠にはしない。公式照合で調整が必要と判明した場合にだけPoCの変換部分を変更し、削除・緩和した制約、公式根拠、その代替となるPython検証をこの表・評価記録・manifestに記載する。Gemini側は変更しない。実際のPLaMo応答が構造・出典検証を通ることは依然未確認で、フラグ追加やモック成功をもって完了扱いにしない。

比較状況と評価項目は [evaluation.md](evaluation.md) を参照。
