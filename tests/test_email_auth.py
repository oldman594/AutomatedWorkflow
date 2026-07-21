from email.message import EmailMessage

import pytest

from app.config import Settings
from app.email_auth import EmailDeliveryError, SMTPVerificationSender, digest_email_code


class FakeSMTP:
    def __init__(self) -> None:
        self.login_values: tuple[str, str] | None = None
        self.message: EmailMessage | None = None

    def login(self, username: str, password: str) -> None:
        self.login_values = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_email_code_digest_is_bound_to_challenge_and_email() -> None:
    digest = digest_email_code("secret", "challenge", "user@example.com", "123456")

    assert digest == digest_email_code("secret", "challenge", "user@example.com", "123456")
    assert digest != digest_email_code("secret", "other", "user@example.com", "123456")
    assert digest != digest_email_code("secret", "challenge", "other@example.com", "123456")


def test_smtp_sender_builds_login_message(monkeypatch) -> None:
    sender = SMTPVerificationSender(
        Settings(
            smtp_host="smtp.example.com",
            smtp_username="autoflow@example.com",
            smtp_password="smtp-secret",
        )
    )
    smtp = FakeSMTP()
    monkeypatch.setattr("app.email_auth.smtplib.SMTP_SSL", lambda *_args, **_kwargs: smtp)

    sender.send_code("developer@example.com", "123456", 600)

    assert smtp.login_values == ("autoflow@example.com", "smtp-secret")
    assert smtp.message is not None
    assert smtp.message["To"] == "developer@example.com"
    assert "123456" in smtp.message.get_content()


def test_smtp_sender_rejects_missing_configuration() -> None:
    with pytest.raises(EmailDeliveryError, match="尚未配置"):
        SMTPVerificationSender(Settings()).send_code("developer@example.com", "123456", 600)
