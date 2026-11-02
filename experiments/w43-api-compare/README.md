# W43 API比較PoC

[Issue #29](https://github.com/YanTKYS/news-watch/issues/29) のGemini `gemini-3.6-flash` / PLaMo `plamo-3.1-prime` 比較用。**2026-11-02の実API比較は両モデルとも成功し、PoCは完了。** その後の運用判断で週刊はPLaMoへ切替済み、月刊はGeminiを維持している。証跡・採用理由・本番反映結果は [evaluation.md](evaluation.md) を参照。

以下は実装・検証時の手順を残したもの。追加比較の実行やPoC拡張を指示するものではなく、本PoC自体は本番のモデル変数・コード・レビュー・インデックスを更新しない。本番切替は別途PR #34で行った。

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

送信は既存PLaMo PoCのリダイレクト禁止 `open_once` を共用する。**再試行を含む `weekly.request_gemini` は使わない。** 両モデルともタイムアウト180秒、1回のActions runで各最大1回・合計最大2回。モデルの自動切替はしない。タイムアウトでもサーバー側処理や利用枠消費はあり得るため、失敗を見て自動・無条件に再実行しない。

## APIなしの準備

Actionsの **W43 API comparison → Run workflow** を既定の `execute=false` で1回起動すると、両モデルの入力を確認し、比較表を作る。モデル選択入力はない。APIキーがあっても送信0回。

ローカルで同じ準備と集計を確認する場合はリポジトリ直下で実行する（既存名は上書きしない）。

```sh
python scripts/w43_batch_compare.py prepare --run-name local-preparation
python scripts/w43_batch_compare.py summarize --run-name local-preparation
python -m unittest discover -s tests -v
```

モデル別の出力は `experiments/w43-api-compare/runs/MODEL/RUN_NAME/`、事前検査と比較表は `runs/comparison/RUN_NAME/`。runはGit管理対象外。従来の `w43_api_compare.py --model ...` の単発CLIも維持するが、通常の両モデル比較は一括workflowを使う。

## 公式仕様・利用条件の確認

実行前に**両モデル**について、公式仕様・対象キーの利用可能モデル・無料枠/料金・利用規約を確認する。接続確認用の追加APIや `/models` は呼ばず、提供元のコンソールと資料を使う。

| モデル | 使用するAPIと確認項目 |
| --- | --- |
| Gemini 3.6 | [モデル公式資料](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)。`generateContent`、`responseJsonSchema`、既存コードの `thinkingConfig.thinkingBudget=2048` / `maxOutputTokens=16384` が許容されるか、対象キーで利用できるかを確認 |
| PLaMo 3.1 Prime | [Chat Completions公式資料](https://docs.plamo.preferredai.jp/ja/api)。`response_format=json_schema`、`strict=true`、enum/配列長/追加プロパティ制約、`max_tokens=16384`、`model`と`finish_reason`の返却形式を確認 |

この環境ではGemini 3.6公式資料の取得がプロキシ403で拒否された。PLaMoも公式サイトは適用済み許可先に含まれず、今回の公式照合は未完了。上表は**実装が送る形式と確認事項**で、確認済みの機能一覧ではない。PLaMo W42の過去成功は参考であり、W43の26記事や今回のAPI可用性の証明ではない。

制約の削除・緩和は現時点で0件（manifestの `schema_adjustments=[]`）。互換性調整が必要なら、このPoCの変換部分だけを変更し、根拠・差分・Python側の代替検証を記録してテストと固定ハッシュをレビューする。本番やW42を変更したり、失敗後に自動で制約を緩めて再送したりしない。

## 1回の手動起動で両モデルを比較

`workflow_dispatch` はmain上の定義を使って手動起動する。一括実行への変更をmainへ配置してから利用する。今回の作業ではPRの作成までとし、マージやAPI実行は行わない。

1. Actionsの **W43 API comparison** を既定の `execute=false` で1回起動し、両モデルの準備・比較表を確認する。
2. Repository Secret `GEMINI_API_KEY` と `PLAMO_API_KEY` を安全に設定する。両者の公式仕様・利用条件・対象キーを確認する。
3. `execute=true` / `spec_confirmed=true` で1回起動すると、同じrun内の2ステップでGemini→PLaMoへ各1回送信する。確認フラグは人による確認の申告であり、自動的な互換性保証ではない。
4. 同じrunのJob SummaryとArtifactで両者の技術指標・候補を比較する。モデルを切り替えるための2回目の起動は不要。

送信前に入力の件数・固定ハッシュ・ID/URL順を両モデル分生成して照合し、仕様確認フラグも検査する。次に、ネットワーク通信しない専用ステップで両Secretの存在を確認する。片方が未設定なら**両APIとも0回**で停止する。キー検査は存在確認であって認証の有効性検査ではなく、対象キーの権限は提供元で確認する。両Secretはこの非通信ステップだけに同時に渡し、実送信する各ステップにはその提供元のSecretだけを注入する。

API試験に進んだ場合、Geminiが503・タイムアウト・構造不正等でもPLaMoを試験する。`continue-on-error` はもう一方へ進むためだけに使い、最後の集計ステップが、どちらかの失敗・欠落を検出して非0で終了する。片方だけ成功なら `partial_failure`、両方失敗なら `failed`、事前検査で止まれば `blocked`。モデル別の失敗を全体成功として隠さない。キャンセル時は新しいAPI送信を開始しない。

ジョブの再実行・再dispatchは**再び各最大1回・合計最大2回**の送信機会を作る。自動再試行ではないが追加の利用枠・費用が発生し得るため、実行済み結果を確認してから明示的に判断する。

## 回収と評価

Artifact名は `w43-comparison-RUN_ID-RUN_ATTEMPT`。成否を問わず、両モデルの次のファイルと `comparison/` の診断だけを1つに回収する（保持14日）。保持期限に頼らず、実行URLと主要指標・人手評価は後続PRで `evaluation.md` に残す。

- `input-manifest.json`: 入力順・ID/URL対応、ハッシュ、スキーマ調整記録。
- `metrics.json`: 固定モデル、HTTP、送信試行回数、秒数、usage、完了理由、検証成否、失敗stage。未知の完了理由は `other` にし、任意文字列を転記しない。
- `report.md`: 実行概要。構造検証だけで品質や採用可否を結論づけない。
- `candidate.md`: **正常完了かつPython検証通過時だけ**生成。IDからPythonが元記事URLを付与する。

`comparison/` には `preflight.json`（両入力・仕様・キー検査の状態）、`comparison.json`、`comparison.md` を保存する。比較表はJob Summaryにも同じ内容を追記する。準備開始・比較表生成の日時（UTC）、各モデルのAPI送信試行数・HTTP・秒数・完了理由・JSON/出典ID検証・usage・候補の有無・正規化したエラー種別・Actionsステップ結果を横並びに示す。送信前の入力一致と、結果manifestの一致を区別する。結果が欠落して確認できない回数・入力一致は `null` / `None` とし、0回や一致と推測しない。

準備不足でAPIを呼ばなかった場合も比較表を作る。モデルの実行前にブロックされた場合はモデル別ファイルが存在しないことがある。checkout・Python起動・出力準備自体が失敗した場合は比較表も作れないため、Actionsのステップ結果を確認する。

両モデルで既存の内容・文字数・出典ID検証を再利用し、PLaMo PoCの厳密な項目集合検査も使う。途中終了・不正JSON・未知/重複出典ID等は候補を保存しない。モデルが返したテキストに使用キーが含まれる場合も保存しない。キー・認証ヘッダー・リクエスト全文・API生応答・エラー本文・例外の生メッセージはログ/Artifactに出さない。usageはモデルごとの定義のまま扱い、単純なトークン差を品質・効率の差としない。

自動比較は**技術指標だけ**で、日本語品質・トピック選定・実務への示唆・出典の事実性を完了扱いにしない。候補は人が確認して `evaluation.md` へ記録し、モデルに採点させる追加APIは呼ばない。両方が失敗した場合は診断を記録して停止する。本番への候補転記、`data/weeks.json` の更新、週刊/月刊のモデル変更・自動フォールバックは、評価後の**別Issueと明示承認**が必要。本PoCには実装しない。
