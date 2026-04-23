from typing import Optional
import os
from pathlib import Path
from dataclasses import dataclass


def getenv_or_default(var_name: str, default):
    value = os.getenv(var_name)
    if value is None:
        return default
    return value


def is_true(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    # 基础应用配置（队名已改为「启明云伴」）
    app_name: str = os.getenv("APP_NAME", "启明云伴 商赛策划平台 V2.3")
    secret_key: str = os.getenv("SECRET_KEY", "change_this_in_production")
    environment: str = os.getenv("APP_ENV", "local")

    # 数据库配置（项目运行必需，之前误删了）
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./app_data/启明云伴.db")

    # 服务配置
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    allow_origins: str = os.getenv("ALLOW_ORIGINS", "*")

    # 大模型配置 - 对接本地 Ollama
    llm_mode: str = os.getenv("LLM_MODE", "openai")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:1234/v1")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "90"))

    # 存储与导出配置
    storage_path: Path = Path(os.getenv("STORAGE_PATH", "app_data/storage"))
    export_path: Path = Path(os.getenv("EXPORT_PATH", "app_data/exports"))
    verification_delivery: str = os.getenv("VERIFICATION_DELIVERY", "console")
    verification_code_expire_minutes: int = int(os.getenv("VERIFICATION_CODE_EXPIRE_MINUTES", "10"))
    verification_cooldown_seconds: int = int(os.getenv("VERIFICATION_COOLDOWN_SECONDS", "60"))

    # 开发相关配置
    dev_show_codes: bool = is_true(os.getenv("DEV_SHOW_CODES", "true"))

    # 邮件配置（不需要可保持默认）
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_ssl: bool = is_true(os.getenv("SMTP_USE_SSL", "true"))
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", "")

    # 品牌配置（队名「启明云伴」）
    sms_provider: str = os.getenv("SMS_PROVIDER", "console")
    brand_name: str = os.getenv("BRAND_NAME", "启明云伴")


# 实例化配置对象
settings = Settings()