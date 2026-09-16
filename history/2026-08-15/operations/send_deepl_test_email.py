from email.message import EmailMessage

from monitor_core.deepl_translation import translate_to_japanese
from monitor_core.mail import SMTPMailer, SMTPSettings


title = "Global markets reopen after an emergency closure"
summary = "This is sample text for verifying automatic Japanese translation. It is not a real alert."
translated = translate_to_japanese([title, summary])

settings = SMTPSettings.from_env()
message = EmailMessage()
message["Subject"] = "【テスト】DeepL日本語翻訳・メール送信確認"
message["From"] = settings.user
message["To"] = settings.recipient
message.set_content(
    "市場監視システムのテストメールです。実際のニュース・特許・速報ではありません。\n\n"
    f"日本語タイトル: {translated[title]}\n"
    f"原題: {title}\n\n"
    f"日本語要約: {translated[summary]}\n"
    f"原文要約: {summary}\n\n"
    "DeepLに接続できない場合でも、実運用メールは原文で継続送信されます。\n"
)
SMTPMailer(settings).send_message(message)
print("test_email_sent")
