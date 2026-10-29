# Sarashina 2026-10-29 再検証の記録

記録日：2026-10-29（日本時間）  
対象：[Issue #24](https://github.com/YanTKYS/news-watch/issues/24)

**結論：Phase 1の再検証を完了。新モデル `sarashina3.2-mini-thinking` でもGitHubホストランナーからHTTP 403が発生し、旧検証と同じレスポンスヘッダーの特徴が再現した。GitHub ActionsからのSarashina API利用は今回も確認できず、Phase 2（W42の品質比較）には進まない。** 拒否の仕組みは特定していない。

## 背景・比較基準

- 以前の検証（2026-10-17頃）：`sarashina3-mini` をGitHub Actions `ubuntu-latest` から呼び出し、最小チャットと週刊形式の双方で **HTTP 403 / Content-Type: text/html / Server: awselb/2.0**。当時のHTML本文は118バイト。参照：[devlog PR #14](https://github.com/YanTKYS/devlog/pull/14) の `notes/free-apis.md`。
- Windows実機では接続できたとの報告があるが、使用キー・同一リクエスト・送信環境の完全一致は確認できていない。この差から拒否原因を断定しない。
- 2026-10-29のSarashinaアップデート後、新モデル `sarashina3.2-mini-thinking` でのアクセス可否を独立PoCから再検証。実験実装は [PR #25](https://github.com/YanTKYS/news-watch/pull/25) で追加した。

## Phase 1：実行結果と証跡

| 項目 | 結果 |
| --- | --- |
| 準備実行 | [Actions #40859124895](https://github.com/YanTKYS/news-watch/actions/runs/40859124895) — **成功**、API送信0回 |
| 実API実行 | [Actions #40859340016](https://github.com/YanTKYS/news-watch/actions/runs/40859340016) — **失敗**、API送信試行1回 |
| 実行日時 | 2026-10-29 19:39頃（JST） |
| 実行環境 | Linux / GitHub Actions `ubuntu-latest`、`main` |
| 対象・方式 | `sarashina3.2-mini-thinking`、`POST /v1/chat/completions` |
| 送信条件 | `execute=true`、Bearer認証、`reasoning_effort=low`、`max_completion_tokens=1024`、`stream=false` |
| HTTP | **403** |
| Content-Type | **`text/html`** |
| Server | **`awselb/2.0`** |
| 応答時間 | **0.68秒** |
| 試行数・再試行 | **1回・リトライなし** |
| 診断結果 | `status=new_chat_failed`、`new_chat_success=false`、`result=http_error` |
| 旧ヘッダー特徴との一致 | `matches_legacy_header_signature=true` |

証跡：実行 [#40859340016](https://github.com/YanTKYS/news-watch/actions/runs/40859340016) の [Artifact `sarashina-phase1-40859340016-1`](https://github.com/YanTKYS/news-watch/actions/runs/40859340016/artifacts/12890079040) 内、`metrics.json` と `report.md`。Artifactは保持期限のある一時成果物（この実行は10日間）なので主要指標を本書に転記した。ログにも `Phase 1 status: new_chat_failed; API requests: 1` と記録された。

**旧モデル（`sarashina3-mini`）と `GET /v1/models` の新規試行は0回。** 新モデルが失敗した時点で後続の送信を停止する設計どおり。W42の15記事も送信していない。APIキー、HTTPエラー本文、チャット応答はArtifactやこの文書に保存していない。

## 旧検証との比較と解釈

| 比較 | 旧検証（2026-10-17頃） | 今回（2026-10-29） |
| --- | --- | --- |
| モデル | `sarashina3-mini` | `sarashina3.2-mini-thinking` |
| GitHubホストランナー | `ubuntu-latest` | `ubuntu-latest` |
| 最小チャット | HTTP 403 | HTTP 403 |
| Content-Type | `text/html` | `text/html` |
| Server | `awselb/2.0` | `awselb/2.0` |
| エラー本文 | HTML、118バイト | **未取得・未保存** |

- **確認できたこと**：新モデルのChat Completionsでも旧403と同じHTTPステータス・レスポンスヘッダー分類を観測した。今回の更新でGitHub Actionsから利用できるようになった、という結果にはならなかった。
- **確認できないこと**：403の発生箇所（ロードバランサ/WAF/認証/API権限など）、送信元IPやネットワーク制限の有無、キーの発行元・有効範囲、旧HTML本文との一致。ヘッダー `awselb/2.0` は原因や拒否主体の確定根拠ではない。今回のHTTP 403が、新モデルの生成品質を示すものでもない。
- **API互換性の評価**：認証以前または認証処理中の拒否も考えられるため、パラメータ受理や `reasoning_effort` の動作までは検証できなかった。送信元の違うWindows実機で成功した報告はあるが、今回のActions成功を示す証拠には使わない。

## Phase 2（W42生成・品質比較）の扱い

Issue #24では、**GitHub Actionsから新モデルのHTTP 200かつ非空・正常完了のチャット応答を確認できた場合に限り**、W42の15記事でGemini・PLaMoと比較することとしていた。

今回はその条件を満たさなかったため、次の作業を**実施せず終了**する。

- 2026-W42の15記事のAPI送信、構造化出力・出典ID検証、候補Markdown生成
- GeminiとPLaMoとの日本語・トピック選定・実務上の示唆・速度・費用の比較
- Sarashinaの本番採用、Geminiからの切替、自動フォールバック

既存のGemini本番運用、PLaMo実験、日刊ログ・週刊/月刊レビュー、公開サイトは変更していない。

## 今後に再検証する場合

1. Sarashina提供元に **GitHub Actions `ubuntu-latest` からの `POST /v1/chat/completions` でHTTP 403 / `text/html` / `awselb/2.0` が返る**事実を伝え、送信元制限・WAF・キー権限・無償ベータの利用条件を確認する。キーやエラー本文を公開Issueに貼らない。
2. 必要があれば、同一キー・同一リクエストをWindows実機や別の信頼済み環境で比較する。ただし本Issueでの追加API実行は行わず、別途範囲・上限を決める。
3. GitHubホストランナーから新モデルの接続成功が得られた場合のみ、W42の品質比較を新しいIssueとして検討する。

**完了判断：GitHub Actionsからの再疎通試験は実施済み・旧403と同じ特徴を再確認。理由は未確定。Phase 2は条件未達として見送り、Issue #24の検証を終了する。**
