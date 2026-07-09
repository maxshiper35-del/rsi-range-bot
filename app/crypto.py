"""Шифрование API-ключей юзеров (Fernet). Нужно только для V2-executor.

Ключ FERNET_KEY генерируется один раз и живёт в окружении VPS:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Открытые ключи в БД не хранятся: в users.api_key_enc / api_secret_enc лежит только шифр.
journal.py и публичный вывод эти поля не читают.
"""
from __future__ import annotations

from app.config import get_settings


def _fernet():
    from cryptography.fernet import Fernet  # ленивый импорт тяжёлой зависимости

    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY не задан — шифрование ключей недоступно")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(token).decode()


def generate_key() -> str:
    """Сгенерировать новый ключ Fernet (для первичной настройки VPS)."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
