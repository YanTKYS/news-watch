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
.venv/bin/python scripts/plamo_weekly_poc.py --execute
```

1回の実行で最大1回、タイムアウト180秒。リトライ・リダイレクト追従・モデル切替・Web検索・元記事取得は行わない。タイムアウトでもサーバー側の処理・課金はあり得るため、失敗したコマンドを自動で繰り返さない。GitHub Actionsの新規ワークフローは追加していない。既存のAPI不要テストにはPoCのモックテストが含まれる。

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

2026-10-28時点のこの環境では、公式資料へのCONNECTがプロキシの403で拒否された。したがって `enum`、`minItems`、`maxItems`、`additionalProperties` 等の対応範囲、`strict`・`max_tokens` の制限、返却モデル名の表現は公式仕様で再確認が必要。実API成功や対応確認済みとは扱わない。調整が必要ならPoCだけを変更し、差分と理由をこの文書・実行manifestに残してモックテストを更新する。

比較状況と評価項目は [evaluation.md](evaluation.md) を参照。
