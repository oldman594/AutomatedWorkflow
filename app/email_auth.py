from __future__ import annotations

import hashlib
import hmac
import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings


class EmailDeliveryError(RuntimeError):
    pass


class SMTPVerificationSender:
    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.security = settings.smtp_security
        self.username = settings.smtp_username
        self.password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password else None
        )
        self.from_email = settings.smtp_from_email or self.username

    def send_code(self, recipient: str, code: str, expires_in_seconds: int) -> None:
        if not self.host or not self.from_email:
            raise EmailDeliveryError("邮件服务尚未配置，请联系管理员")
        if bool(self.username) != bool(self.password):
            raise EmailDeliveryError("邮件服务账号配置不完整")
        message = EmailMessage()
        message["Subject"] = "AutoFlow 登录验证码"
        message["From"] = self.from_email
        message["To"] = recipient
        minutes = max(1, expires_in_seconds // 60)
        message.set_content(
            f"你的 AutoFlow 登录验证码是：{code}\n\n验证码 {minutes} 分钟内有效，请勿转发给他人。"
        )
        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=15) as smtp:
                    self._deliver(smtp, message)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                    smtp.starttls(context=context)
                    self._deliver(smtp, message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError("验证码邮件发送失败，请稍后重试") from exc

    def _deliver(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self.username and self.password:
            smtp.login(self.username, self.password)
        smtp.send_message(message)


def digest_email_code(secret: str, challenge_id: str, email: str, code: str) -> str:
    payload = f"{challenge_id}:{email}:{code}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
