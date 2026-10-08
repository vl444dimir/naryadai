from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    database_url: str = "postgresql+psycopg://naryadai:naryadai@127.0.0.1:54339/naryadai"
    upload_dir: Path = Path("storage/photos")
    cors_origins: list[str] = ["http://localhost:5173"]
    session_hours: int = 12
    worker_enabled: bool = False
    worker_interval_seconds: float = 2
    reminder_minutes: int = 30
    accept_minutes: int = 10
    emergency_accept_minutes: int = 3
    repeat_minutes: int = 30
    leader_escalation_minutes: int = 120
    max_photo_bytes: int = 10 * 1024 * 1024
    ai_enabled: bool = False
    ai_provider: str = "openai"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = ""
    ai_api_key: str = ""
    ai_allow_real_data: bool = False
    ai_timeout_seconds: float = 45
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    public_app_url: str = "http://localhost:5173"
    demo_password: str = "Demo-Only-2026!"


@lru_cache
def get_settings() -> Settings:
    return Settings()
