from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from monitor_core.mail import SMTPMailer, SMTPSettings


def main() -> int:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    subject = f"[最新論文モニター] 診断メール {now:%Y-%m-%d %H:%M} JST"
    body = "\n".join(
        [
            "最新論文モニターのメール送信テストです。",
            "",
            "SMTP接続・認証・送信経路は正常に動作しています。",
            f"送信日時: {now:%Y-%m-%d %H:%M:%S} JST",
            "",
            "このメールは論文候補の通知ではありません。",
        ]
    )
    SMTPMailer(SMTPSettings.from_env()).send_text(subject, body)
    print("TEST_EMAIL=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
