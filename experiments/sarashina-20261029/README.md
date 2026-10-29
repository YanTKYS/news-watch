# Sarashina 2026-10-29 疎通再検証（Phase 1）

[Issue #24](https://github.com/YanTKYS/news-watch/issues/24) の最優先課題であるGitHubホストランナーからのアクセス再検証を行う独立PoC。**2026-10-29にGitHub Actionsで実APIを1回試し、HTTP 403を再確認したため、Phase 1の再検証は終了した。Phase 2（W42生成）は実施しない。** 詳細な実測値・比較判断は [evaluation.md](evaluation.md) を参照。

本番のBrave/Jev/Gemini、PLaMo実験、日刊ログ、レビュー、インデックス、公開サイトは変更しない。スクリプトは標準ライブラリだけを使い、既存データを読み書きしない。

## リクエストと停止条件

| 順番 | 操作 | 送信条件 |
| --- | --- | --- |
| 1 | 新モデル `sarashina3.2-mini-thinking` で小さい日本語チャット | `execute=true` とキーが必要 |
| 2 | 旧モデル `sarashina3-mini` で同じチャット | 1のHTTP 200、非空の本文、`finish_reason=stop`を確認した場合のみ |
| 3 | 任意の `GET /v1/models` | 1が成功し、`include_models=true` の場合のみ |

チャットは `POST https://api.sarashina.ai/v1/chat/completions`、Bearer認証、`stream=false`、`max_completion_tokens=1024`。新モデルにだけ `reasoning_effort=low` を送る。旧式の `max_tokens` は使わない。プロンプトは公開可能な固定の接続確認文だけで、記事・個人情報・非公開情報は送らない。応答本文は成功時も保存しない。

無償ベータでは利用データがサービス改善等に使われる可能性がある。実行前に利用規約・データの取り扱いを確認し、公開サンプル以外の入力へ変更しない。

最大3回（GETも含む）、各タイムアウト90秒。リトライ・リダイレクト追従はなく、上限は送信処理側でも検査する。旧モデルの試験は失敗時の代替生成ではなく、Issueに指定された独立した比較である。

新モデルはHTML 403の既知パターンだけでなく、401/JSON 403/429/その他エラー、空の本文、途中終了、不正JSON、サイズ超過でも1回で停止し、旧モデルとModels APIを呼ばない。旧モデルの失敗時は、任意のModels APIを1回実行して診断する。Modelsの成功だけでチャット成功とは判定しない。

## 手動Actions

1. 初回はこのワークフローを含むPRを先にマージする。新規 `workflow_dispatch` はデフォルトブランチに存在する必要があり、PRブランチの指定だけでは手動起動できない（PR #25で実施済み）。
2. **Settings → Secrets and variables → Actions** のRepository Secretに `SARASHINA_API_KEY` を安全に登録する。キーをチャット・コード・ログに貼らない。既存キーを使う場合は発行元と権限を確認する。
3. **Actions → Sarashina 20261029 connectivity PoC → Run workflow** で、まず `execute=false` の準備実行を行い、診断Artifactを確認する。キーがあっても既定では送信しない。
4. 公式仕様・利用規約とキーの利用先を確認後、`execute=true` を指定して1回だけ起動する。`include_models=false` ならGETを省略できる。APIの自動起動はschedule/push/pull_requestのいずれにも設定していない。
5. 成功・失敗にかかわらずArtifact `sarashina-phase1-RUN_ID-RUN_ATTEMPT` を回収する。今回は [Actions #40859340016](https://github.com/YanTKYS/news-watch/actions/runs/40859340016) の実測値を [evaluation.md](evaluation.md) に記録済み。再実行でリクエスト枠はリセットされるため、同じ条件で自動・無条件に再実行しない。

実API用Secretは実行ステップだけに渡す。キー未設定なら送信前に終了コード2で停止し、試行0回のArtifactを残す。ワークフローはcontents読み取り権限だけで、生成物のcommit/pushは行わない。Artifactの設定は14日保持だが、今回のActionsログではリポジトリ上限により**10日保持**に短縮された。主要な実測値は [evaluation.md](evaluation.md) に保存した。

## ローカルでの確認

リポジトリ直下での準備とテスト（APIなし）:

```sh
python experiments/sarashina-20261029/probe.py
python -m unittest discover -s tests -v
```

安全な環境変数でキーを設定し `--execute` を付けると、ローカルから最大3回送信する。任意のGETを省く場合は `--skip-models`。ただし、**ローカルの接続成功をGitHubホストランナーの成功証跡として扱わない**。ローカルの実API試験は今回行っていない。

## 診断Artifactと分類

`runs/<UTC時刻>/` または `--run-name NAME` のディレクトリに、`README.md`、`metrics.json`、`report.md` の3ファイルだけを保存する。既存runの上書き・パストラバーサルを拒否する。runはGit管理対象外で、本番パスは出力先にしない。

- `metrics.json`: 送信試行回数、各試行の種類・HTTPステータス/分類、Content-Type/Serverの分類、経過秒、新モデルのチャット成否、OS・runnerラベル。回数は送信試行数であり、サーバー受信や処理完了を保証しない。
- Content-Typeは `application/json`、`application/problem+json`、`text/html` の既知の種類のみ、Serverは `awselb/2.0`、`nginx`、`cloudflare` の既知値のみ保存する。パラメータを除き、未知の文字列は `other`、欠落は `missing` に正規化する。任意ヘッダー値に秘密情報が含まれても転記しない。
- `matches_legacy_header_signature=true` はHTTP 403 / text/html / awselb/2.0の一致を表す。本文や本文サイズは取得・照合せず、旧118バイトのHTMLと同一だったという主張もしない。WAF、送信元制限、認証等の原因は断定できない。
- `new_chat_success` は完了したチャット応答の構造検査。200だけでは成功にしない。旧モデルやModelsの結果が失敗ならジョブは失敗になるが、新モデルの成否は別項目に残す。
- runnerはこのワークフローから `ubuntu-latest` を指定するラベルで、実行URLと実OSを照合する。外部IPの取得リクエストは行わない。

キー、Bearerヘッダー、リクエスト全文、応答本文、例外の生メッセージはログ・Artifactに保存しない。HTTPエラー本文は読み取らず破棄する。診断の保存に失敗した場合もAPIを自動再実行しない。

## 仕様と次の段階

参考は [2026-10-29公式発表](https://www.sbintuitions.co.jp/blog/entry/2026/10/29/133000)、[Chat Completions](https://docs.sarashina.ai/api-reference/chat-completions)、[Models API](https://docs.sarashina.ai/api-reference/models)。初期実装時のクラウド環境では公式ドキュメントの取得がプロキシの403で拒否されたため、Issueに指定されたパラメータを採用した。その後の実APIはHTTP 403で拒否され、パラメータの受理・生成品質は未検証。モック成功を互換性の証明にしない。

Phase 1で新モデルのActions接続は成功しなかったため、**今回のIssueではPhase 2へ進まない**。将来接続成功が確認できれば、別Issue・別PR・別手動実行として検討する。W42の15記事、既存 `weekly.py` の読み込み・プロンプト・ID・Python検証・Markdown生成を再利用し、SarashinaのSchema対応を確認してPoC内でのみ変換する。reasoning effortを1種類に固定し、最大8000トークン以内の上限を確認、追加APIは1回・再試行なしとする。今回は新モデルで403が再現したためW42生成は行わず、診断と次の確認事項を [evaluation.md](evaluation.md) に記録して検証を終了した。
