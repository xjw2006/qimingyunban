from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path.cwd()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "POIUY 商赛策划平台 V2.3")
    secret_key: str = os.getenv("SECRET_KEY", "change_this_in_production")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./app_data/poiuy.db")
    app_env: str = os.getenv("APP_ENV", "local")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    allow_origins: str = os.getenv("ALLOW_ORIGINS", "*")
    llm_mode: str = os.getenv("LLM_MODE", "mock")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "90"))
    storage_path: Path = ROOT_DIR / os.getenv("STORAGE_PATH", "app_data/storage")
    export_path: Path = ROOT_DIR / os.getenv("EXPORT_PATH", "app_data/exports")
    verification_delivery: str = os.getenv("VERIFICATION_DELIVERY", "console")
    verification_code_expire_minutes: int = int(os.getenv("VERIFICATION_CODE_EXPIRE_MINUTES", "10"))
    verification_cooldown_seconds: int = int(os.getenv("VERIFICATION_COOLDOWN_SECONDS", "60"))
    dev_show_codes: bool = _bool(os.getenv("DEV_SHOW_CODES", "true"), True)
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_ssl: bool = _bool(os.getenv("SMTP_USE_SSL", "true"), True)
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", "noreply@example.com")
    sms_provider: str = os.getenv("SMS_PROVIDER", "console")
    brand_name: str = os.getenv("BRAND_NAME", "POIUY Strategy Studio")

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"local", "dev", "development", "debug"}


settings = Settings()
settings.storage_path.mkdir(parents=True, exist_ok=True)
settings.export_path.mkdir(parents=True, exist_ok=True)
