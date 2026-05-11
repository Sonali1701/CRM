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
    # Auto-create first admin on startup if DB is empty
    admin_email: str = ""
    admin_password: str = ""
    admin_name: str = "Admin"


@lru_cache
def get_settings() -> Settings:
    return Settings()
