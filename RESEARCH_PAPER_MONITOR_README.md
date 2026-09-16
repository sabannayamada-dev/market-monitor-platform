# 最新論文モニター

OpenAlexとarXivから、研究室紹介資料を基にした「電磁波計測・可視化・宇宙電波工学」「ダイヤモンド半導体・パワーデバイス」「ダイヤモンド量子センサ・表面界面解析」の新着メタデータを収集し、SQLiteで重複排除して日本語メールを作る独立サービスです。PDF本文は取得・保存しません。

ダイヤモンド系は題名中のdiamond/NV系中心語と具体的研究対象の両方を必須とし、電波系も電磁界計測、EMI源探査、衛星搭載計測、宇宙プラズマなどの具体語を必須とします。一般的な半導体、通信、DFT、放射線というだけの論文は掲載対象になりません。

## 段階運用

- `PAPER_OPERATION_STAGE=1`: 収集と保存
- `PAPER_OPERATION_STAGE=2`: ルール採点
- `PAPER_OPERATION_STAGE=3`: 上位候補をOpenAIで日本語要約（API障害時はルール結果を維持）
- `PAPER_OPERATION_STAGE=4`: 診断付き日次メール

初回実行は設定された30日間をベースラインとして保存し、メールを送りません。2回目以降は直近7日間を重複込みで再検索し、初めて発見した論文だけを候補にします。

HTTP 403/429では同一run内の即時再送を止め、API別の休止終了時刻をSQLiteへ保存します。初回6時間、連続時は12、24、48時間へ延長し、`Retry-After`がそれより長ければその値を優先します。休止中の手動再実行はそのAPIをスキップし、もう一方のAPIだけを継続します。成功後に連続429状態を解除します。

## ローカル確認

```bash
python research_paper_preflight.py
python research_paper_daily.py --baseline
python -m unittest test_research_paper_monitor.py
```

本番では `/etc/patent-news-monitor.env` にパスと資格情報を設定し、最初はstage 1で収集品質を確認してください。stage 4へ進める前にSMTP設定を確認します。

## 主なデータ

- `papers`: API横断で統合した論文
- `source_records`: OpenAlex/arXivの取得元レコード
- `paper_scores`: 決定論的な分野別スコアと理由
- `ai_reviews`: 日本語要約、重要度、費用
- `collection_runs` / `source_runs`: 収集診断
- `notification_outbox`: 送信成功後だけsentになるメールキュー
