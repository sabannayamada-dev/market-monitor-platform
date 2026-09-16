# 就活向け 企業ランキング Streamlit Cloud版

このフォルダは、個人用のランキング作成アプリとは別に作ったデプロイ用の軽量版です。

## 内容

- `company_ranking_cloud.py`: Streamlitアプリ本体
- `data/company_data.csv.gz`: `4500+7.csv` を gzip 圧縮した内蔵データ
- `requirements.txt`: Streamlit Community Cloud用の依存関係
- `.streamlit/config.toml`: 画面テーマ設定

## デプロイ手順

1. この `streamlit_cloud_app` フォルダの中身をGitHubリポジトリに入れる
2. Streamlit Community Cloudでリポジトリを選択
3. Main file path に `company_ranking_cloud.py` を指定
4. Deploy を押す

## 注意

- EDINET APIキーや収集機能は含めていません。
- データは `data/company_data.csv.gz` に固定されています。
- 新しいデータに差し替える場合は、同じ列構成のCSVを gzip 化してこのファイルを置き換えてください。
