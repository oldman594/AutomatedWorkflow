from __future__ import annotations

import hashlib
import hmac
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings

logger = logging.getLogger(__name__)


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
        self._validate_configuration()
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
            with self._connection(context) as smtp:
                if self.security == "starttls":
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                self._deliver(smtp, message)
        except (OSError, smtplib.SMTPException) as exc:
            logger.exception(
                "smtp_delivery_failed",
                extra={
                    "smtp_host": self.host,
                    "smtp_port": self.port,
                    "smtp_security": self.security,
                },
            )
            raise EmailDeliveryError("验证码邮件发送失败，请稍后重试") from exc

    def probe(self) -> str:
        """Connect, negotiate TLS and authenticate without sending a message."""
        self._validate_configuration()
        context = ssl.create_default_context()
        try:
            with self._connection(context) as smtp:
                if self.security == "starttls":
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.noop()
        except (OSError, smtplib.SMTPException) as exc:
            logger.exception(
                "smtp_probe_failed",
                extra={
                    "smtp_host": self.host,
                    "smtp_port": self.port,
                    "smtp_security": self.security,
                },
            )
            raise EmailDeliveryError("SMTP 连接或认证失败，请检查主机、TLS 模式和授权码") from exc
        return f"SMTP {self.security} connection and authentication succeeded"

    def _validate_configuration(self) -> None:
        if not self.host or not self.from_email:
            raise EmailDeliveryError("邮件服务尚未配置，请联系管理员")
        if bool(self.username) != bool(self.password):
            raise EmailDeliveryError("邮件服务账号配置不完整")

    def _connection(self, context: ssl.SSLContext):
        assert self.host is not None
        if self.security == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=15)
        return smtplib.SMTP(self.host, self.port, timeout=15)

    def _deliver(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self.username and self.password:
            smtp.login(self.username, self.password)
        smtp.send_message(message)


def digest_email_code(secret: str, challenge_id: str, email: str, code: str) -> str:
    payload = f"{challenge_id}:{email}:{code}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
