from __future__ import annotations

import hashlib
import json
import random
import secrets
from datetime import datetime, timezone

from redis import Redis
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    validate_password_or_raise,
    verify_password,
)
from app.models.user import User


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def issue_token_pair(redis: Redis, user_id: int) -> dict:
    access_token, access_jti, access_exp = create_access_token(user_id)
    refresh_token, refresh_jti, refresh_exp = create_refresh_token(user_id)

    # Store refresh token as a session in Redis
    refresh_ttl_seconds = int((refresh_exp - datetime.now(timezone.utc)).total_seconds())
    redis.setex(f"refresh:{refresh_jti}", refresh_ttl_seconds, str(user_id))

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_jti": access_jti,
        "access_exp": access_exp,
        "refresh_jti": refresh_jti,
        "refresh_exp": refresh_exp,
    }


def revoke_refresh_token(redis: Redis, refresh_jti: str) -> None:
    redis.delete(f"refresh:{refresh_jti}")


def blacklist_access_token(redis: Redis, access_jti: str, access_exp: datetime) -> None:
    ttl = int((access_exp - datetime.now(timezone.utc)).total_seconds())
    if ttl < 1:
        return
    redis.setex(f"access_blacklist:{access_jti}", ttl, "1")


def is_access_token_blacklisted(redis: Redis, access_jti: str) -> bool:
    return redis.exists(f"access_blacklist:{access_jti}") == 1


def create_email_verification(redis: Redis, user: User) -> dict:
    token = secrets.token_urlsafe(32)
    code = f"{random.randint(0, 999999):06d}"

    payload = {
        "token_hash": _sha256(token),
        "code_hash": _sha256(code),
        "email": user.email,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ttl_seconds = settings.email_verify_ttl_min * 60
    redis.setex(f"email_verify:{user.id}", ttl_seconds, json.dumps(payload))

    # Resend cooldown
    redis.setex(
        f"email_verify_cooldown:{user.id}",
        settings.email_verify_resend_cooldown_seconds,
        "1",
    )

    return {"token": token, "code": code, "ttl_min": settings.email_verify_ttl_min}


def can_resend_email_verification(redis: Redis, user_id: int) -> bool:
    return redis.exists(f"email_verify_cooldown:{user_id}") == 0


def verify_email(redis: Redis, user: User, token: str | None = None, code: str | None = None) -> bool:
    raw = redis.get(f"email_verify:{user.id}")
    if not raw:
        return False
    data = json.loads(raw)

    if token:
        if _sha256(token) != data.get("token_hash"):
            return False
    elif code:
        if _sha256(code) != data.get("code_hash"):
            return False
    else:
        return False

    # Success - delete key
    redis.delete(f"email_verify:{user.id}")
    return True


def create_password_reset(redis: Redis, user: User) -> dict:
    token = secrets.token_urlsafe(32)
    payload = {
        "token_hash": _sha256(token),
        "email": user.email,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ttl_seconds = settings.password_reset_ttl_min * 60
    redis.setex(f"pwd_reset:{user.id}", ttl_seconds, json.dumps(payload))
    return {"token": token, "ttl_min": settings.password_reset_ttl_min}


def verify_password_reset(redis: Redis, user: User, token: str) -> bool:
    raw = redis.get(f"pwd_reset:{user.id}")
    if not raw:
        return False
    data = json.loads(raw)
    if _sha256(token) != data.get("token_hash"):
        return False
    redis.delete(f"pwd_reset:{user.id}")
    return True


def register_user(db: Session, *, email: str, password: str) -> User:
    validate_password_or_raise(password)

    email_norm = email.strip().lower()
    existing = db.query(User).filter(User.email == email_norm).first()
    if existing:
        raise ValueError("Email already registered")

    user = User(
        email=email_norm,
        password_hash=hash_password(password),
        email_verified=False,
        is_admin=False,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User | None:
    email_norm = email.strip().lower()
    user = db.query(User).filter(User.email == email_norm).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
