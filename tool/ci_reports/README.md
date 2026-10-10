
# Flutter CI reports

既存のFlutterテストは変更せず、`testing_app/test` のUnit・Widgetテストを実行します。
既存 `main.yml` は upstream `flutter/samples` 専用なので変更しません。
Integration Testは端末・エミュレータを必要とするため、この最小構成の対象外です。
`resolution: workspace` に合わせて依存解決はリポジトリルートで行います。

## GitHubで実行

1. 2つの新規Workflowと `tool/ci_reports` をmainへ反映します。
2. ForkのActionsが無効ならActionsタブで有効化します。
3. **Flutter Test Report** を手動実行、またはmainへのpush/PRで実行します。
4. 実際の失敗が出たら同じRunの **Re-run failed jobs** を実行します。
   新規Runの成功は同じRunの再実行ではありません。失敗を人工的に埋め込みません。
5. **Flutter CI Analysis** を手動実行します。main上では毎時17分（1時間ごと）にも動きます。
6. `flutter-ci-report-*` Artifactをダウンロードし、`report.html` を開きます。

テストの失敗終了コードはpipefailで維持します。成功・失敗にかかわらずmachineイベントを
`flutter-tests-<run_id>-<run_attempt>` に90日保存します（セットアップ失敗時は存在しません）。
安定版Flutterを使用します。SDKや外部依存の変化もFAIL→PASSの原因となり得ます。
再現性が必要になった時点で検証済みFlutterバージョンの固定とlockfileの運用を検討してください。

## ローカル実行

Python 3.9以降:

```sh
python3 -m venv build/ci-venv
. build/ci-venv/bin/activate
python -m pip install -r tool/ci_reports/requirements.txt
python tool/ci_reports/report.py --repo inaina12345/flutter_samples --output build/ci-report
python -m unittest discover -s tool/ci_reports/tests -v
```

公開リポジトリのRun/Job取得は認証なしでも可能ですがAPI制限があります。
通常は環境変数 `GITHUB_TOKEN` に対象リポジトリの **Actions: read** のみのトークンを設定します。
Artifactから履歴を復元する `--restore-latest` は認証が必要です。
トークンをコードやコマンドライン引数に埋め込まないでください。
Workflowは追加後にGitHubへ登録されます。追加前に実行するとWorkflow APIは404になります。

```sh
# 前回の出力を継続利用（同じoutputならhistory.jsonを自動読込）
python tool/ci_reports/report.py --repo inaina12345/flutter_samples --days 365
# 別の保存先から履歴を引き継ぐ
python tool/ci_reports/report.py --repo inaina12345/flutter_samples --history archive/history.json
# GitHubへアクセスせず再描画
python tool/ci_reports/report.py --repo inaina12345/flutter_samples --history archive/history.json --offline
# テストArtifactを展開したmachineイベントから遅い個別テストを一覧化
python tool/ci_reports/report.py --repo inaina12345/flutter_samples --offline --machine tests.jsonl
```

## 出力と判定範囲

- `history.json`: 累積Run/Attempt/Job記録。同じキーは最新結果で更新。
- `attempts.csv`: 全収集AttemptのJob・テストステップ結果、所要時間、SHA、リンク。
- `recoveries.csv`: 同一Run・SHA・JobのテストステップがFAIL→PASSとなった候補。
- `weekly.csv`: 月曜始まりUTCの週次成功・失敗・その他・回復候補数。
- `report.html`: Plotlyを内包したオフライン閲覧可能な週次・長期所要時間グラフ。
- `test-durations.csv`: `--machine` 指定時のみ個別テストの時間（hidden/skip除外）。

FAIL→PASSは**テストステップ単位の回復候補**です。Flakyな個別テストと断定しません。
pub getやSDKセットアップ失敗はテスト失敗として数えません。
部分再実行で実行されなかったJobを成功で埋めず、実際に取得したJobのみ比較します。
複数の失敗後の成功は直近の失敗Attemptと対にします。キャンセル等は成功と扱いません。
個別テストの過去ログ横断比較・Flaky率・自動再実行はこの基礎実装には含めません。

分析Workflowは前回成功した同Workflowのmain上Artifactからhistory.jsonを復元して追加します。
継続実行される限り90日を超えた収集記録も引き継げますが、Artifact自体は永続保管ではありません。
90日以上の中断、削除、保持設定変更に備えてhistory.jsonを外部へ定期保管してください。
履歴がない場合は新規収集を開始します。実行済みデータ0件は「観測なし」であり「Flakyなし」ではありません。
API取得失敗は処理を失敗させ、不完全な取得を成功レポートとして上書きしません。
初回は更新日が過去365日以内のRunを収集し、その全Attemptをページング取得します。
大規模リポジトリではAPI制限・30分のWorkflow時間制限に注意し、`--days`を調整してください。

## 検証用サンプル

`examples/history.json` は架空の6 Attempt（回復候補2件）です。実際のCI履歴ではありません。

```sh
python tool/ci_reports/report.py --repo inaina12345/flutter_samples \
  --history tool/ci_reports/examples/history.json --offline --output build/example-report
```
