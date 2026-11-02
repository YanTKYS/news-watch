# W43 API比較PoC

[Issue #29](https://github.com/YanTKYS/news-watch/issues/29) のGemini `gemini-3.6-flash` / PLaMo `plamo-3.1-prime` 比較用。**実APIは未実施。** 本番のモデル変数・週刊/月刊コード・定期workflow・レビュー・インデックスは変更しない。結果・採用判断は [evaluation.md](evaluation.md) に記録する。

## 固定入力と再利用

対象は2026-W43（10月19〜25日に取得）の26記事。既存 `weekly.py` の読み込み・選別・ID付与・プロンプト・スキーマ・Markdown生成を使う。記事本文の追加取得やWeb検索はしない。

`input-manifest.json` に順序付きの記事ID/URLと次のハッシュを保存し、件数だけでなく記事内容・順序・プロンプト・Schemaの変化も送信前に拒否する。ハッシュは `plamo_weekly_poc.digest` による、UTF-8・sort_keys付きJSONのSHA-256。

| 対象 | 固定SHA-256 |
| --- | --- |
| 記事ペイロード | `6e629ed58a4b246aa4e78f8b1b0f4c6235ebfed1c04dffdc1b0dcef5f9e79de9` |
| systemプロンプト | `c30a72eb14f6c5fed7a968ed1b4aec89a231ada871ea6c50e2c7ab34a76d880b` |
| userプロンプト | `9cb3e8014fc7f80d342940e3228f16cd42b393a059e7cfdffdcb2e816686bd6f` |
| JSON Schema | `300748dc2c4aaddc36555eecaa0d0299e4780fbd8e33a983203e10ed0ee6b184` |

Geminiのリクエストは `weekly.build_request` にモデル名を明示して作る。Repository variable/環境変数 `GEMINI_MODEL` に依存せず、その値も変更しない。PLaMoは既存 `plamo_weekly_poc.build_request` を使い、W42専用の `prepare` や `run` は呼ばない。W42の15記事・ハッシュはそのまま残す。

送信は既存PLaMo PoCのリダイレクト禁止 `open_once` を共用する。**再試行を含む `weekly.request_gemini` は使わない。** 両モデルともタイムアウト180秒、各runで最大1回、モデル切替なし。タイムアウトでもサーバー側処理や利用枠消費はあり得るため、失敗を見て自動・無条件に再実行しない。

## APIなしの準備

リポジトリ直下で実行する。モデルごとに結果を分けるので、両方の準備をしてもAPIは呼ばない。

```sh
python scripts/w43_api_compare.py --model gemini-3.6-flash
python scripts/w43_api_compare.py --model plamo-3.1-prime
python -m unittest discover -s tests -v
```

出力は `experiments/w43-api-compare/runs/MODEL/UTC時刻/`。`--run-name NAME` も使えるが、既存runの上書きは禁止する。runはGit管理対象外。

## 公式仕様・利用条件の確認

実行前に選択モデルについて、公式仕様・対象キーの利用可能モデル・無料枠/料金・利用規約を確認する。接続確認用の追加APIや `/models` は呼ばず、提供元のコンソールと資料を使う。

| モデル | 使用するAPIと確認項目 |
| --- | --- |
| Gemini 3.6 | [モデル公式資料](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)。`generateContent`、`responseJsonSchema`、既存コードの `thinkingConfig.thinkingBudget=2048` / `maxOutputTokens=16384` が許容されるか、対象キーで利用できるかを確認 |
| PLaMo 3.1 Prime | [Chat Completions公式資料](https://docs.plamo.preferredai.jp/ja/api)。`response_format=json_schema`、`strict=true`、enum/配列長/追加プロパティ制約、`max_tokens=16384`、`model`と`finish_reason`の返却形式を確認 |

この環境ではGemini 3.6公式資料の取得がプロキシ403で拒否された。PLaMoも公式サイトは適用済み許可先に含まれず、今回の公式照合は未完了。上表は**実装が送る形式と確認事項**で、確認済みの機能一覧ではない。PLaMo W42の過去成功は参考であり、W43の26記事や今回のAPI可用性の証明ではない。

制約の削除・緩和は現時点で0件（manifestの `schema_adjustments=[]`）。互換性調整が必要なら、このPoCの変換部分だけを変更し、根拠・差分・Python側の代替検証を記録してテストと固定ハッシュをレビューする。本番やW42を変更したり、失敗後に自動で制約を緩めて再送したりしない。

## 独立した手動実行

新規 `workflow_dispatch` はmainに存在する必要があるため、**このPRを配置・マージしてから**手動実行する。今回の作業ではPRの作成までとし、マージやAPI実行は行わない。

1. Actionsの **W43 API comparison** を選び、モデルを1つ選ぶ。既定の `execute=false` で準備を確認する。
2. 対象のRepository Secret `GEMINI_API_KEY` または `PLAMO_API_KEY` を安全に設定する。Secretは対象モデルの実行ステップだけに注入し、同時には渡さない。
3. 選択モデルの公式仕様・利用条件を確認した場合だけ、`execute=true` / `spec_confirmed=true` を指定して1回起動する。確認フラグは人による確認の申告であり、自動的な互換性保証ではない。
4. 結果を回収してから、もう一方のモデルは**別の明示的な手動操作**で実行する。一方の失敗で他方を起動する機能はない。ジョブ再実行・再dispatchは別のAPI消費になる。

同じ指定はローカルでも可能だが、接続許可と安全に注入した環境変数が必要。例は選択モデルに対する1回の実API操作で、準備手順の一部として実行しない。

```sh
python scripts/w43_api_compare.py --model gemini-3.6-flash --execute --spec-confirmed
```

キー未設定、仕様未確認、入力不一致は送信前に停止する。CLIのキー/仕様によるブロックは診断ファイルに記録する。workflowの仕様確認ガードや、入力不一致・出力準備失敗でrunを作れない場合はArtifactが存在しないことがあるが、送信は行わない。

## 回収と評価

Artifact名は `w43-MODEL-RUN_ID-RUN_ATTEMPT`。成否を問わず、次のファイルだけを回収する（保持14日）。保持期限に頼らず、実行URLと主要指標・人手評価は後続PRで `evaluation.md` に残す。

- `input-manifest.json`: 入力順・ID/URL対応、ハッシュ、スキーマ調整記録。
- `metrics.json`: 固定モデル、HTTP、送信試行回数、秒数、usage、完了理由、検証成否、失敗stage。未知の完了理由は `other` にし、任意文字列を転記しない。
- `report.md`: 実行概要。構造検証だけで品質や採用可否を結論づけない。
- `candidate.md`: **正常完了かつPython検証通過時だけ**生成。IDからPythonが元記事URLを付与する。

両モデルで既存の内容・文字数・出典ID検証を再利用し、PLaMo PoCの厳密な項目集合検査も使う。途中終了・不正JSON・未知/重複出典ID等は候補を保存しない。モデルが返したテキストに使用キーが含まれる場合も保存しない。キー・認証ヘッダー・リクエスト全文・API生応答・エラー本文・例外の生メッセージはログ/Artifactに出さない。usageはモデルごとの定義のまま扱い、単純なトークン差を品質・効率の差としない。

両方が失敗した場合は診断を記録して停止する。本番への候補転記、`data/weeks.json` の更新、週刊/月刊のモデル変更・自動フォールバックは、評価後の**別Issueと明示承認**が必要。本PoCには実装しない。
