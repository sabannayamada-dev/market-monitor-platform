from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    user: str
    password: str
    recipient: str

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port and self.user and self.password and self.recipient)

    @classmethod
    def from_env(cls) -> "SMTPSettings":
        user = os.getenv("SMTP_USER", "").strip()
        return cls(
            host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
            port=int(os.getenv("SMTP_PORT", "587")),
            user=user,
            password=os.getenv("SMTP_PASSWORD", "").strip(),
            recipient=os.getenv("EMAIL_RECIPIENT", user).strip(),
        )


class SMTPMailer:
    """Single SMTP implementation shared by every monitor."""

    def __init__(self, settings: SMTPSettings, timeout_seconds: int = 30):
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def send_message(self, message: EmailMessage) -> None:
        if not self.settings.configured:
            raise RuntimeError("SMTP is not configured")
        context = ssl.create_default_context()
        with smtplib.SMTP(
            self.settings.host, self.settings.port, timeout=self.timeout_seconds
        ) as server:
            server.starttls(context=context)
            server.login(self.settings.user, self.settings.password)
            server.send_message(message)

    def send_text(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.user
        message["To"] = self.settings.recipient
        message.set_content(body)
        self.send_message(message)
