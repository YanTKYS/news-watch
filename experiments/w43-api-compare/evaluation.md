# W43 Gemini 3.6 / PLaMo 比較記録

更新日: 2026-11-03（日本時間）

**比較PoCは完了。** W43の26記事による実API比較でGemini 3.6 Flash / PLaMo 3.1 Primeの両方が成功した。その後のGemini本番実行で503が続いたため、利用者の方針として追加比較を打ち切り、**週刊はPLaMoを採用・本番反映済み。月刊はGeminiを維持する。**

## 比較条件と実測の証跡

[Issue #29](https://github.com/YanTKYS/news-watch/issues/29) / PR #30・#32で、取得日2026-10-19〜25の26記事、ID・URL・順序・system/userプロンプト・JSON Schemaを固定した。入力とハッシュの定義は[README](README.md#固定入力と再利用)を正本とする。記事本文の追加取得やWeb検索は行わない。

- [比較実行 #41461518681](https://github.com/YanTKYS/news-watch/actions/runs/41461518681)：2026-11-02 23:23 JST頃に手動起動、`main`、実行結果 `success`（Actions APIで確認）。
- [Artifact `w43-comparison-41461518681-1`](https://github.com/YanTKYS/news-watch/actions/runs/41461518681/artifacts/13148249564)：両モデルのmanifest・指標・候補と比較表の保存先。保持期間は14日。
- HTTP・試行数・検証結果は[利用者の結果整理コメント](https://github.com/YanTKYS/news-watch/issues/29#issuecomment-6581124999)に基づく。本改訂ではArtifactの存在・未期限切れをAPIで確認したが、配布先への通信が403で拒否され、内容を再取得できなかった。**正確な所要時間・usageは推測して転載せず、既存Artifactを参照先とする。**

| 項目 | Gemini 3.6 Flash | PLaMo 3.1 Prime |
| --- | --- | --- |
| モデルID | `gemini-3.6-flash` | `plamo-3.1-prime` |
| 入力 | W43・固定26記事 | W43・固定26記事 |
| 比較実行 | #41461518681、成功 | #41461518681、成功 |
| API送信試行 | 1回 | 1回 |
| HTTP | 200 | 200 |
| JSON構造・出典ID検証 | 合格 | 合格 |
| 正確な秒数・usage | 既存Artifact参照（今回未転記） | 既存Artifact参照（今回未転記） |

構造・出典ID検証の成功は、本文の事実性やすべてのSchema制約をAPI側だけで強制できることの保証ではない。W43のGemini 3.8版は生成されておらず、成功済みの比較対象として扱わない。[PLaMo W42評価](../plamo-w42/evaluation.md)は15記事での参考記録であり、今回の26記事の速度・品質を代替しない。

## 採用理由と本番反映

比較では両モデルとも成功したが、その直後の[Gemini 3.6本番週刊 #41463290641](https://github.com/YanTKYS/news-watch/actions/runs/41463290641)は失敗した（Actions APIで確認）。503が2回発生した内訳は上記コメントに基づく。比較時の1回成功だけでは、本番での継続的な成功を保証できなかった。

利用者は、W43の復旧を優先し、W42・W43の実API PoCで構造化出力と出典ID検証に成功したPLaMoを採用した。**文章品質・費用の厳密な優劣を確定したための採用ではなく、生成を復旧するための運用判断**である。PLaMoの長期可用性も未保証だが、このIssueで追加比較や別モデル選定を続けない。

- [PR #34](https://github.com/YanTKYS/news-watch/pull/34) は2026-11-03 JSTにマージ済み。週刊だけ `plamo-3.1-prime` / `PLAMO_API_KEY` に切り替え、月刊のGemini処理・設定は維持した。
- [W43本番実行 #41468214396](https://github.com/YanTKYS/news-watch/actions/runs/41468214396)：2026-11-03 00:05 JST頃に手動起動、`success`（Actions APIで確認）。[2026-W43本文](../../reviews/weekly/2026-W43.md)の生成モデルはPLaMo、入力26件、生成日は2026-11-03。
- [2026-W44本文](../../reviews/weekly/2026-W44.md)も存在し、生成モデルPLaMo、入力21件、生成日2026-11-03を確認した。W43・W44はいずれも[data/weeks.json](../../data/weeks.json)に登録済み。

比較PoCのPLaMo候補には出典と説明の整合性に確認事項があったため、**検証成功を理由に無審査で公開へコピーする扱いにはしなかった**。本番反映は通常の週刊生成で行い、PoC候補と本番本文を同一視しない。生成物の構造検証は、元記事の独立した事実確認を代替しない。

## 完了範囲と留保

W43の固定入力による両APIの実測、成否の記録、利用者による採用判断、別Issue #33 / PR #34を通した本番反映まで確認できた。日本語品質・主要事例の選定・実務への示唆・費用の厳密な優劣や長期安定性は断定しないが、これらを理由に比較PoCを未完了とは扱わない。

本改訂は既存の記録と生成物に基づく文書整合性修正のみで、追加API実行・モデル再選定・PoC拡張は行っていない。この修正PRのマージ時にIssue #29をcompletedとして閉じる。週刊の継続運用・次回定期実行の確認は、Issue #29のクローズ条件へ追加しない。
