from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from redis import Redis
from sqlalchemy.orm import Session

from app.api.deps import get_current_token_payload, get_current_user, get_db, get_redis
from app.core.config import settings
from app.core.email_utils import send_email
from app.core.security import decode_token, validate_password_or_raise
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RequestPasswordResetRequest,
    ResetPasswordRequest,
    ResendVerificationRequest,
    TokenPair,
    VerifyEmailRequest,
)
from app.schemas.user import UserPublic
from app.services.auth_service import (
    authenticate_user,
    blacklist_access_token,
    can_resend_email_verification,
    create_email_verification,
    create_password_reset,
    issue_token_pair,
    register_user,
    revoke_refresh_token,
    verify_email,
    verify_password_reset,
)


router = APIRouter(prefix="/auth", tags=["auth"])


def _send_verification_email(to_email: str, user_id: int, token: str, code: str) -> None:
    verify_link = f"{settings.public_base_url_clean}/verify.html?uid={user_id}&token={token}"

    subject = f"[{settings.app_name}] 验证你的邮箱"
    text = (
        f"你好！\n\n"
        f"请使用以下验证码完成邮箱验证：{code}\n\n"
        f"或者直接点击链接：\n{verify_link}\n\n"
        f"如果不是你本人操作，请忽略此邮件。\n"
    )

    html = (
        f"<p>你好！</p>"
        f"<p>请使用以下验证码完成邮箱验证：</p>"
        f"<h2 style='letter-spacing:2px'>{code}</h2>"
        f"<p>或直接点击：</p>"
        f"<p><a href='{verify_link}'>验证邮箱</a></p>"
        f"<p>如果不是你本人操作，请忽略此邮件。</p>"
    )

    send_email(to_email, subject, text, html=html)


def _send_password_reset_email(to_email: str, user_id: int, token: str) -> None:
    reset_link = f"{settings.public_base_url_clean}/reset.html?uid={user_id}&token={token}"

    subject = f"[{settings.app_name}] 重置密码"
    text = (
        f"你好！\n\n"
        f"请点击链接重置密码：\n{reset_link}\n\n"
        f"如果不是你本人操作，请忽略此邮件。\n"
    )

    html = (
        f"<p>你好！</p>"
        f"<p>请点击链接重置密码：</p>"
        f"<p><a href='{reset_link}'>重置密码</a></p>"
        f"<p>如果不是你本人操作，请忽略此邮件。</p>"
    )

    send_email(to_email, subject, text, html=html)


@router.post("/register")
def register(
    payload: RegisterRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    try:
        user = register_user(db, email=payload.email, password=payload.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    verify = create_email_verification(redis, user)

    background.add_task(_send_verification_email, user.email, user.id, verify["token"], verify["code"])

    resp = {
        "message": "Registered. Verification email sent.",
        "user_id": user.id,
    }

    # Dev convenience: show token/code in API response (do NOT enable in prod)
    if settings.app_env == "dev":
        resp.update({"dev_verify_token": verify["token"], "dev_verify_code": verify["code"]})

    return resp


@router.post("/resend-verification")
def resend_verification(
    payload: ResendVerificationRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = db.query(User).filter(User.email == payload.email.strip().lower()).first()
    # Avoid user enumeration
    if not user:
        return {"message": "If the account exists, an email has been sent."}

    if user.email_verified:
        return {"message": "Email already verified."}

    if not can_resend_email_verification(redis, user.id):
        raise HTTPException(status_code=429, detail="Too many requests. Please try later.")

    verify = create_email_verification(redis, user)
    background.add_task(_send_verification_email, user.email, user.id, verify["token"], verify["code"])

    resp = {"message": "Verification email sent."}
    if settings.app_env == "dev":
        resp.update({"dev_verify_token": verify["token"], "dev_verify_code": verify["code"]})
    return resp


@router.post("/verify-email")
def verify_email_endpoint(
    payload: VerifyEmailRequest,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.email_verified:
        return {"message": "Email already verified."}

    ok = verify_email(redis, user, token=payload.token, code=payload.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token/code")

    user.email_verified = True
    db.add(user)
    db.commit()

    return {"message": "Email verified."}


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = authenticate_user(db, email=payload.email, password=payload.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is disabled")

    if user.banned_until and user.banned_until > datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail="User is banned")

    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email not verified")

    tokens = issue_token_pair(redis, user.id)

    return LoginResponse(
        tokens=TokenPair(access_token=tokens["access_token"], refresh_token=tokens["refresh_token"]),
        user=UserPublic.model_validate(user),
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(
    payload: RefreshRequest,
    redis: Redis = Depends(get_redis),
):
    try:
        decoded = decode_token(payload.refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if decoded.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    refresh_jti = decoded.get("jti")
    user_id = decoded.get("sub")
    if not refresh_jti or not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # Check session exists
    stored = redis.get(f"refresh:{refresh_jti}")
    if not stored or stored != str(user_id):
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    # Rotate
    revoke_refresh_token(redis, refresh_jti)
    new_tokens = issue_token_pair(redis, int(user_id))

    return TokenPair(access_token=new_tokens["access_token"], refresh_token=new_tokens["refresh_token"])


@router.post("/logout")
def logout(
    payload: LogoutRequest,
    redis: Redis = Depends(get_redis),
    token_payload=Depends(get_current_token_payload),
):
    # Revoke refresh token
    try:
        decoded_refresh = decode_token(payload.refresh_token)
    except Exception:
        decoded_refresh = None

    if decoded_refresh and decoded_refresh.get("type") == "refresh":
        rjti = decoded_refresh.get("jti")
        if rjti:
            revoke_refresh_token(redis, rjti)

    # Blacklist current access token until it expires
    access_jti = token_payload.get("jti")
    exp = token_payload.get("exp")
    access_exp = None
    if isinstance(exp, (int, float)):
        access_exp = datetime.fromtimestamp(exp, tz=timezone.utc)
    elif isinstance(exp, str):
        # jose might return str
        try:
            access_exp = datetime.fromtimestamp(float(exp), tz=timezone.utc)
        except Exception:
            access_exp = None

    if access_jti and access_exp:
        blacklist_access_token(redis, access_jti, access_exp)

    return {"message": "Logged out"}


@router.get("/me", response_model=UserPublic)
def me(user: User = Depends(get_current_user)):
    return UserPublic.model_validate(user)


@router.post("/request-password-reset")
def request_password_reset(
    payload: RequestPasswordResetRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = db.query(User).filter(User.email == payload.email.strip().lower()).first()

    # Always return ok to avoid user enumeration
    if not user:
        return {"message": "If the account exists, a reset email has been sent."}

    reset = create_password_reset(redis, user)
    background.add_task(_send_password_reset_email, user.email, user.id, reset["token"])

    resp = {"message": "If the account exists, a reset email has been sent."}
    if settings.app_env == "dev":
        resp.update({"dev_reset_token": reset["token"], "user_id": user.id})
    return resp


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = db.query(User).filter(User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ok = verify_password_reset(redis, user, payload.token)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    try:
        validate_password_or_raise(payload.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.core.security import hash_password

    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    db.commit()

    return {"message": "Password updated"}
