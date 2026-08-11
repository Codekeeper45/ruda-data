from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AppSetting


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def set_setting(session: Session, key: str, value: str, *, encrypted: bool = False) -> None:
    stored = _fernet().encrypt(value.encode("utf-8")).decode("ascii") if encrypted else value
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=stored, encrypted=encrypted)
        session.add(row)
    else:
        row.value = stored
        row.encrypted = encrypted


def get_setting(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None:
        return default
    if not row.encrypted:
        return row.value
    try:
        return _fernet().decrypt(row.value.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return default


def get_api_key(session: Session) -> str | None:
    if settings.speechmatics_api_key:
        return settings.speechmatics_api_key
    return get_setting(session, "speechmatics_api_key")


def get_openrouter_api_key(session: Session) -> str | None:
    if settings.openrouter_api_key:
        return settings.openrouter_api_key
    return get_setting(session, "openrouter_api_key")
