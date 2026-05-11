from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./crm.db"
    stale_deal_days: int = 5
    env: str = "development"
    session_cookie_name: str = "crm_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14  # 14 days
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant_id: str = "common"
    ms_redirect_uri: str = ""
    mail_sync_key: str = ""
    mail_sync_min_interval_seconds: int = 120
    # SMTP fallback — used when no Microsoft Graph mailbox is connected
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # defaults to smtp_user if blank
    smtp_use_tls: bool = True  # True = STARTTLS on 587; False = SSL on 465
    # Default admin: created on startup if missing. Override via env vars.
    admin_email: str = "admin@radixsol.com"
    admin_password: str = "admin123"
    admin_name: str = "Admin"


@lru_cache
def get_settings() -> Settings:
    return Settings()
