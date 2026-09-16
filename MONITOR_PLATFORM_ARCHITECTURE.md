# 市場監視プラットフォーム構成

## 目的

底検知、特許材料性、将来追加するTDnet・GDELT・決算・指値提案を、
同じ運用規約で安全に動かすための共通基盤です。各アルゴリズムのDBと
通知キューは分離し、共通化するのは運用上重複していた部分だけです。

## 境界

```text
systemd timer
  -> monitorctl.py（共通ランナー）
       -> preflight（設定・接続前検査）
       -> domain worker（収集・判定・通知）
       -> platform.sqlite3（共通実行履歴）

domain worker
  -> collector（外部データ取得）
  -> algorithm（判定）
  -> domain outbox（重複防止・再送）
  -> monitor_core.mail（SMTP送信）
```

## 共通化したもの

- `monitorctl.py`: サービスの起動、事前検査、時間制限、履歴記録
- `monitor_services.json`: サービスID、実行ファイル、予定時刻の台帳
- `monitor_core/locking.py`: 重複起動防止と所有者情報付きロック
- `monitor_core/mail.py`: Gmail SMTPの共通実装
- `monitor_core/journal.py`: 全サービス共通の実行履歴とイベント
- `/var/lib/market-monitor/platform.sqlite3`: VPS上の共通運用DB

## 分離したままにするもの

- 株価キャッシュと底判定アルゴリズム
- 底判定の通知済み・ベースラインDB
- 特許、AI評価、株価答え合わせDB
- 特許の日次ダイジェスト・緊急通知outbox

通知状態をサービス間で共有しないため、別サービスの処理が通知済み状態を
誤って更新することはありません。

## 守るべき実装規約

1. 同じ入力に対する識別キーを固定し、再実行しても重複通知しない。
2. 通知は先にoutboxへ保存し、送信成功後にだけsentへ変更する。
3. 収集済みデータは差分更新し、重い過去データを毎回取り直さない。
4. workerの前にpreflightを置き、資格情報・DB・書込み先を検査する。
5. ドメインDBと共通運用DBを分ける。
6. 新しい判定ロジックはメールやsystemdへ直接依存させない。
7. API制限、タイムアウト、部分失敗を結果として記録する。
8. 本番タイマーを変更する前に、構文検査、単体テスト、preflightを通す。

## 新サービスの追加手順

例としてTDnet監視を追加する場合:

1. `tdnet_preflight.py`を作る。
2. `tdnet_daily.py`を作る。
3. TDnet専用DBに通知outboxと通知済みキーを持たせる。
4. `monitor_services.json`へ次を追加する。

```json
"tdnet-disclosure": {
  "label": "TDnet適時開示モニター",
  "schedule": "平日 20:15 Asia/Tokyo",
  "preflight": ["tdnet_preflight.py"],
  "command": ["tdnet_daily.py"],
  "timeout_seconds": 3600,
  "state_hint": "TDNET_STATE_DB"
}
```

5. `ExecStart=.../python -u monitorctl.py run tdnet-disclosure`のoneshot serviceと
   timerを追加する。
6. `monitorctl.py validate`、`monitorctl.py preflight tdnet-disclosure`、テストを実行する。

## 運用コマンド

```bash
python monitorctl.py list
python monitorctl.py validate
python monitorctl.py preflight stock-bottom
python monitorctl.py status
python monitorctl.py history patent-materiality --limit 10
```

`platform.sqlite3`はサービス横断の稼働確認用です。判定内容や通知再送は、
各サービスの専用DBを正として扱います。
