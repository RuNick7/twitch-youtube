from __future__ import annotations

from cryptography.fernet import Fernet


class Vault:
    """Шифрует токены перед записью в базу."""

    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise SystemExit("SECRET_KEY должен быть 32 байтами в urlsafe base64, см. .env.example") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode()).decode()
