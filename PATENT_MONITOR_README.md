# 特許材料性モニター v0.2.0

上場企業の新着特許を取得し、安価なローカル処理で候補を絞ってから、一定以上だけGeminiへ送る独立アプリです。

## 実装済みの流れ

1. CSVまたはEPO OPSから特許を取得
2. `family_id`、優先権番号、合成キーの順で重複・特許ファミリーを統合
3. 出願人を上場企業マスタへ名寄せ
4. CPC/IPC、対象語、除外語で対象技術を評価
5. ファミリー規模、国数、引用、請求項、法的状態でメタデータ評価
6. SQLite内の過去特許と文字3-gram類似度・企業初CPCを比較して新規性評価
7. 時価総額と共同出願人数を加味して材料性評価
8. 総合閾値、企業内percentile、企業初CPC、棄却標本監査のいずれかでGeminiへ送信
9. 全判断根拠とGemini構造化JSONをSQLite/CSVへ保存

Geminiへ送る前のスコアは、投資リターンや特許価値を直接予測するモデルではありません。AI審査の対象を安く絞るための一次スクリーニングです。閾値の妥当性は運用データで継続検証してください。

## 起動

Anaconda Promptでこのフォルダへ移動し、次を実行します。

```powershell
python patent_monitor_tool.py
```

または `launch_patent_monitor.bat` を開きます。外部Pythonライブラリは不要です。

最初は入力元を `モック` にして実行すると、APIキーなしで全工程を確認できます。モック選択中は、画面にGeminiキーが保存されていても外部APIを呼びません。対象候補は `gemini_pending` としてCSVへ残ります。v0.1.6のモックは、実際の企業マスタに合わせた上場企業、和英別名、同一特許ファミリー、対象外技術、未一致出願人に加え、過去に株価反応が確認された日東精工の特許第7301490号を含みます。既知の株価反応はGeminiへ渡しません。

GUIを使わず、既存履歴を汚さずにモックのE2E検証だけを行う場合は次を実行します。

```powershell
python validate_patent_monitor_mock.py
```

すべて正常ならJSONの先頭に `"passed": true` と表示されます。一時フォルダ内で実行するため、通常の履歴DBと出力フォルダは変更しません。

## 入力元

### CSV

`sample_patents.csv` が最小例です。列名には日本語・英語の主要な別名を許容します。重要な列は次の通りです。

- `publication_number`
- `family_id` または `priority_number`
- `title`, `abstract`, `claims`
- `applicants`
- `cpc_codes`, `ipc_codes`
- `publication_date`
- `country_codes`, `citation_count`, `family_size`, `claim_count`, `legal_status`

複数値は `|` で区切ります。

### EPO OPS

EPO OPSのConsumer Key/Secretを詳細設定へ入力します。APIキーはGUI状態ファイルへ保存しません。日付だけで検索する場合、CQLは次の形で自動生成されます。

```text
pd within "20260713 20260714"
```

任意CQLを指定したい場合は詳細設定の `EPO CQL` を使います。レートは設定JSONの `ops_requests_per_minute` で抑制します。

### JPO APIについて

JPOの特許情報取得APIは、既知の出願番号を起点に経過、書類、登録、OPDファミリー等を取得する性格が強く、新着公報の発見フィードにはそのまま使いにくいAPIです。このためv0.1.0では、探索をCSV/EPO OPS、JPOを将来の詳細補強器として追加できる構造にしています。

## 企業マスタ

`patent_company_master.csv` を編集します。

- `company_id`: 永続的な社内ID
- `company_name`: 表示名
- `ticker`: 証券コード
- `aliases`: 英語名、旧社名などを `|` 区切り
- `subsidiaries`: 親会社材料として扱う子会社名を `|` 区切り
- `market_cap_jpy`: 円単位の時価総額
- `target`: `1`なら対象
- `technology_tags`: 企業固有の注目技術語

完全一致を優先し、曖昧なファジー一致は自動採用せず `company_match_review.csv` へ送ります。子会社特許を親会社へ寄せるかは企業マスタで明示します。

## 初回運用

新規性は企業の過去特許と比べるため、過去1～3年分をCSV等で一度投入すると精度が上がります。履歴がない企業の初回特許は、新規性を中立値50点として扱います。過去データの初回投入時はGeminiキーを空欄にし、履歴だけ構築する運用が安全です。

## Gemini費用制御

- `gemini_threshold`: 全体スコアの送信閾値
- `company_percentile_threshold`: 企業内で例外的に高い特許を救う閾値
- `random_reject_audit_rate`: 棄却候補も一部だけAIへ送り、見逃し率を監査
- `monthly_gemini_limit`: 月間API呼び出し上限
- `gemini_model`: 使用モデル。既定は`gemini-3.1-flash-lite`。`auto`では利用可能な低コストFlash Lite系を優先し、停止・新規受付終了モデルは除外

APIキーは環境変数 `GEMINI_API_KEY` でも渡せます。呼び出し回数と入出力文字数はSQLiteの `api_usage` に月別保存されます。

Windows版では「Windowsユーザー用に暗号化して保存」を有効にすると、GeminiおよびEPO OPSの資格情報をWindows DPAPIで暗号化します。暗号化ファイルは `%LOCALAPPDATA%/PatentMaterialityMonitor/credentials.dpapi` に置かれ、原則として保存を実行したWindowsユーザーだけが復号できます。ソースコード、通常設定JSON、GUI状態ファイルには平文キーを書きません。

## 出力

実行ごとに `outputs/patent_monitor/<run_id>/` を作ります。

- `patent_evaluations.csv`: 全候補、全段階スコア、経路、エラー
- `gemini_reviewed_or_pending.csv`: Gemini評価済み・送信待ち・APIエラー
- `company_match_review.csv`: 名寄せ確認待ち・未一致
- `run_summary.json`: バージョン、件数、経路別集計
- `patent_monitor.sqlite3`: 長期履歴、評価版、API利用量

過去評価は上書きしません。`app_version`、`prompt_version`、`model` を記録するため、モデル更新前後を比較できます。

## デバッグ

テストは次で実行できます。

```powershell
python -m unittest -v test_patent_monitor.py
```

エラーはバッチ全体を止めず、可能な限り対象特許の `error` 列に残します。Geminiの通信失敗は `gemini_error`、月間上限は `gemini_limit`、APIキー未設定は `gemini_pending` です。

## 次の拡張点

- JPO APIによる出願経過、拒絶理由、引用、OPDファミリーの補強
- 上場企業マスタの自動更新と、企業規模の時点整合
- Gemini通過後のGPT再審査
- 通常ダイジェストと緊急通知を分けたメール送信
- 棄却監査結果を使う閾値校正と、企業・業種別の動的閾値

GPT再審査とメール送信は、v0.1.4では意図的に実行しません。Geminiの `decision` と `email_summary` はSQLiteへ保存済みなので、次段を別モジュールとして安全に追加できます。
