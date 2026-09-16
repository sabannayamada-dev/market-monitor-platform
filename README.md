# 企業評価スコアリングツール

求人データ、法人番号、EDINET情報を結合し、重み付きスコアで企業をランキングする試作品です。

現在のバージョン: `v0.11.38`

バージョンはアプリ画面のヘッダー右側、`outputs\run_manifest.json`、`outputs\self_check_report.json` にも記録されます。

## すぐ起動する

Windowsでダブルクリック:

```text
launch_company_scoring_tool.bat
```

Anaconda Promptから起動:

```powershell
python company_scoring_tool.py
```

XBRL解析は `lxml` が入っている環境では自動的に高速パーサーを使います。入っていない場合も標準パーサーへ戻るため、そのまま動作します。

## 普段使いの流れ

起動すると、最初に `いつも使う` 画面が表示されます。

普段使う操作は次の4つです。

- `EDINETから情報を収集開始`: EDINET APIから企業情報を収集します
- `遡る日数`: 何日前までのEDINET書類を探すか指定します
- `最大取得件数`: 一度に取得する上限を指定します
- `詳しい数値を取る企業数`: 平均年収などの詳しい財務データを何社分まで取りに行くか指定します
- `分析候補外をCSVに入れない`: 投資信託系や法人番号なしなどを普段開く企業一覧CSVから外します
- `収集した最近のCSVファイルを開く`: 直近の企業一覧CSVを開きます

収集したデータは `outputs\collections` に蓄積されます。新しい収集をしても、過去の収集データは消さずに残します。

`止めるまで収集し続ける` モードでは、個別の収集CSVは保存単位ごとに保存します。大量データ時の停止を避けるため、全体の集約CSVは通常は中断時にまとめて更新します。

## Google Trends結合

`いつも使う` 画面の `Google Trends収集・結合（pytrends）` から、EDINET収集済みCSVなどへ検索関心の時系列要約を追加できます。

1. `結合したいCSV` を選択
2. 取得期間、最大企業数、検索語の末尾を設定
3. `Google Trendsを収集・結合` を押す

出力は `outputs\pytrends_enrichment` に保存されます。結合CSV、全時系列CSV、失敗一覧CSV、7日間再利用するキャッシュDBを作成します。

pytrendsはGoogle公式APIではありません。レート制限や仕様変更で失敗する場合があるため、最初は最大企業数を100以下にしてください。また、0〜100の指数は各企業の検索語ごとに正規化されるため、企業間の絶対検索量比較には利用できません。

## 詳細分析の流れ

1. `launch_company_scoring_tool.bat` を起動
2. 必要な場合だけ `詳細分析` 画面を開く
3. 求人ファイルに `work\sample_job_input.csv` または収集済みCSVを指定
4. 必要なら法人番号マスタに `work\sample_corporate_master.csv` を指定
5. 重みを画面で調整
6. `入力チェック` と `列チェック` でファイルの状態を確認
7. `設定保存` を押す
8. `実行` を押す
9. ランキングプレビューと注意が必要な行を確認
10. 必要なら `Excelを開く` または `CSVを開く`

## 設定プリセット

`work` フォルダにサンプル設定があります。

- `scoring_config_standard.json`: 標準
- `scoring_config_rd_focus.json`: 研究開発重視
- `scoring_config_wage_focus.json`: 給与・実質時給重視
- `scoring_config_growth_test.json`: 昇給ポテンシャル重視のテスト用

GUIの設定ファイル欄で選び、`設定読込` を押すと画面へ反映できます。

## 入力テンプレート

実データを作るときは、次のテンプレートから始めると列名のズレを減らせます。

- `work\job_input_template.csv`
- `work\corporate_master_template.csv`

詳しくは `outputs\input_file_format_guide.md` を確認してください。

## 主な出力

出力は `outputs` フォルダに保存されます。

- `company_scoring_result_YYYYMMDD.xlsx`
- `ranking.csv`
- `all_records_clean.csv`
- `all_records_clean.jsonl`
- `score_components.csv`
- `errors.jsonl`
- `run_manifest.json`
- `logs`
- `collections\collected_companies.csv`
- `collections\collected_companies_all.csv`
- `collections\analysis_candidate_companies.csv`
- `collections\all_collected_records.csv`
- `collections\collection_quality_summary.csv`

普段開く収集系CSVは日本語列名で出力します。内部処理で再利用する `collections\{collection_run_id}\normalized_records.csv` とJSONLは、プログラム連携しやすいよう英語キーのまま残します。

EDINET収集では、XBRL取得上限の範囲で次の財務項目もCSVに保存します。

- 平均年間給与
- 平均勤続年数
- 研究開発費
- 売上高
- 従業員数
- 主要設備などのテキスト情報
- 総資産、純資産、負債、自己資本比率
- 営業利益、経常利益、当期利益、粗利益
- 営業/投資/財務キャッシュフロー、現金同等物
- ROE、EPS、1株当たり純資産、配当、配当性向
- 資本金、設備投資、平均年齢
- 女性管理職比率、男性育休取得率、男女賃金差異
- 事業内容、リスク、配当方針、研究開発、人的資本方針などのテキスト情報
- 証券コード、対象期間、書類説明、XBRL/PDF/CSVフラグなどのEDINET書類メタ情報

銀行・保険・証券・IFRS適用企業では、売上高の代替として経常収益、営業収益、Revenue系タグも探索します。
投資信託、受益証券、資産流動化証券、投資事業権利など、事業会社分析に向かない書類は書類説明や様式コードから分析候補外にします。

## 注意行の見方

「注意が必要な行」には、ランキングに入らない理由や確認が必要な行が出ます。

- `edinet_record_not_found`: EDINET側に法人番号一致がない
- `missing_corporate_number`: 求人側に法人番号がない
- `estimated_number_name_mismatch`: 補完した法人番号でJOINできたが、企業名が一致しない
- `insufficient_data`: スコア計算に必要なデータが足りない

怪しい行は消さずに `all_records_clean.csv` に残し、ランキングからは除外する設計です。

## EDINET APIを試す

Anaconda PromptでAPIキーを設定します。

```powershell
$env:EDINET_API_KEY="ここにAPIキー"
```

最初はドライランで試します。

GUIでは、APIキー欄にキーを入れて `EDINET接続確認` を押すと、1日分だけ書類一覧の取得を確認できます。

```powershell
python company_scoring.py --job-file work\sample_job_input.csv --use-edinet-api --edinet-dry-run --edinet-lookback-days 7
```

GUIの接続確認結果は `outputs\edinet_connection_test` に保存されます。

## 収集ジョブを試す

CSV/Excelを収集ジョブとして取り込み、raw/normalized/manifestを作れます。

GUIでは `大量収集開始` ボタンから実行できます。収集したデータは `outputs\collections\{collection_run_id}` に保存され、既存の収集runは削除しません。同じ入力ファイルを再収集した場合も、新しいrunとして保存し、manifestに過去runとの関係を記録します。

法人番号マスタを指定している場合は、収集時に `corporate_number_review_queue.csv` も作成します。法人番号が空の行について、候補件数、最有力候補、確認理由を残すため、後でJOIN失敗の原因を追いやすくなります。

各収集runは個別フォルダに残しつつ、横断分析用に `outputs\collections\all_collected_records.csv` と `outputs\collections\all_collected_records.jsonl` も自動更新します。シェルスクリプトや別プログラムからは、この統合ファイルを読むと扱いやすいです。

企業単位で見たい場合は `outputs\collections\collected_companies.csv` を使います。EDINETの書類一覧を法人番号または企業名で集約し、書類件数、最新書類ID、EDINETコードを確認できます。

GUIには `最新収集CSVを使う`、`法人番号確認CSVを開く`、`収集フォルダを開く`、`全収集CSVを開く`、`企業一覧CSVを開く` ボタンがあります。

収集後は品質確認用に `collection_quality_summary.csv`、`collection_quality_summary.json`、`duplicate_audit.csv` も作成します。法人番号なし、収集警告、重複候補をまとめて確認できます。

収集元は画面の `収集元` で選べます。現時点で実行できるのは `job_file: 求人CSV/Excel入力` と `edinet_api: EDINET API v2` です。EDINET収集にはAPIキーが必要です。国税庁法人番号API、求人APIは台帳に候補として登録済みですが、実装・利用条件確認待ちです。

`EDINET APIを使う` にチェックが入っている状態で `大量収集開始` を押すと、収集元をEDINETへ自動的に切り替えます。EDINET収集結果は提出企業候補として保存しますが、求人ファイル欄は上書きしません。求人の給与・休日と組み合わせた分析には、別途求人CSVまたは求人API由来のデータが必要です。

EDINET収集の `record_count` は企業数ではなく書類件数です。同じ企業が複数書類を出すため、企業数を見る場合は `推定ユニーク企業数` または `unique_company_count` を確認してください。

### 止めるまでEDINET収集

`いつも使う` 画面で `止めるまで収集し続ける` をオンにして `EDINETから情報を収集開始` を押すと、遡る日数や最大取得件数を指定せず、`収集中断` が押されるまでEDINETを日付単位で集め続けます。

このモードでは1日分を処理するたびに `outputs\collections\all_collected_records.csv`、`collected_companies.csv`、`analysis_candidate_companies.csv` などの統合CSVを定期保存します。再開情報は `outputs\collections\continuous_edinet_state.json` に残るため、アプリを閉じた後でも次回起動時に同じモードで開始すれば続きから収集できます。再開時は、まず最新日付側の未収集分を確認してから、前回の古い日付方向の続きへ戻ります。

収集済み `normalized_records.csv` をもう一度大量収集しようとした場合は警告します。通常は、収集済みCSVを使って `実行` を押すと分析できます。

```powershell
python company_collection.py --registry work\data_source_registry_template.json --source-id job_file --corporate-master work\sample_corporate_master.csv
```

または:

```text
run_collection_sample.bat
```

出力は `outputs\collections\{collection_run_id}` に保存されます。

## しょくばらぼCSVで職場情報を追加する

厚生労働省「職場情報総合サイト しょくばらぼ」のCSV一括ダウンロードを、EDINET収集済み企業CSVへ法人番号で結合できます。有給休暇取得率、残業時間、男女賃金差、育休、女性管理職、テレワーク、副業、研修制度など、EDINETだけでは取りにくい働きやすさ系の列を追加します。

```powershell
python shokuba_enrichment.py --shokuba-file C:\Users\saban\Downloads\T_SHOKUBA_20260705\shokuba_20260705.csv --company-file outputs\collections\collected_companies.csv
```

出力は `outputs\shokuba_enrichment` に保存されます。主に使うのは、重要指標だけを抜いた `companies_with_shokuba_summary_metrics.csv` と、広めに列を残した `companies_with_shokuba_key_metrics.csv` です。

GUIでは `いつも使う` 画面の `しょくばらぼ結合` から実行できます。`しょくばらぼCSV` に一括ダウンロードCSVを指定し、`結合したいCSV` に法人番号列を含む任意のCSVを指定して `しょくばらぼ情報を結合` を押します。`最新の企業一覧CSVを使う` を押すと、直近のEDINET収集結果CSVを結合対象にできます。

## トラブル時

依存ライブラリ不足:

```powershell
pip install -r requirements.txt
```

自己診断:

```powershell
python self_check.py
```

または:

```text
run_self_check.bat
```

配布前の監査:

```powershell
python release_audit.py
```

または:

```text
run_release_audit.bat
```

確認するファイル:

- `outputs\run_manifest.json`
- `outputs\errors.jsonl`
- `outputs\logs`
- `outputs\self_check_report.json`
- `outputs\release_audit_report.json`
- `outputs\prototype_test_plan.md`

## 配布用ZIPを作る

配布用に必要ファイルだけをまとめる場合:

```powershell
python package_release.py
```

または:

```text
package_release.bat
```

ZIPは `outputs\dist` に保存されます。

## 現時点の未完成部分

- ハローワークAPI連携
- EDINET XBRL解析の精度改善
- 国税庁APIなどによる法人番号補完
- EXE化
