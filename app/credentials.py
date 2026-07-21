from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings


class CredentialVault:
    def __init__(self, settings: Settings) -> None:
        if not settings.credential_encryption_key:
            raise RuntimeError("AUTOFLOW_CREDENTIAL_ENCRYPTION_KEY is not configured")
        try:
            self.fernet = Fernet(
                settings.credential_encryption_key.get_secret_value().encode("ascii")
            )
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("Credential encryption key is invalid") from exc

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("Stored Git credential cannot be decrypted") from exc
