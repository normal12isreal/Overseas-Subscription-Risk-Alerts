from pathlib import Path
import os
import tomllib

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todo_directory: Path
    data_directory: Path = Path("data")
    escalation_hours: float = Field(default=3, gt=0)
    refresh_hours: float = Field(default=24, gt=0)
    max_parallel_events: int = Field(default=2, ge=1, le=8)
    max_parallel_branches: int = Field(default=3, ge=1, le=12)
    max_videos: int = Field(default=5, ge=1, le=5)
    branch_timeout_seconds: float = Field(default=90, gt=0)
    tool_timeout_seconds: float = Field(default=25, gt=0)
    llm_timeout_seconds: float = Field(default=40, gt=0)
    llm_max_output_tokens: int = Field(default=1400, ge=256, le=4096)
    llm_max_calls: int = Field(default=30, ge=1, le=100)
    transcript_max_characters: int = Field(default=12000, ge=100, le=50000)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "qwen-flash"
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    apprise_config: Path | None = None
    gmail_credentials_file: Path | None = None
    gmail_token_file: Path = Path("data/gmail-token.json")
    gmail_max_messages: int = Field(default=10, ge=1, le=100)
    mail_max_characters: int = Field(default=12000, ge=1000, le=16000)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        path = path.resolve()
        load_dotenv(path.parent / ".env", override=False)
        with path.open("rb") as handle:
            values = tomllib.load(handle)
        for field in ("llm_base_url", "llm_api_key", "llm_model",
                      "telegram_bot_token", "telegram_chat_id", "apprise_config",
                      "gmail_credentials_file", "gmail_token_file"):
            value = os.getenv(field.upper())
            if value:
                values[field] = value
        settings = cls.model_validate(values)
        for field in ("todo_directory", "data_directory", "apprise_config",
                      "gmail_credentials_file", "gmail_token_file"):
            value = getattr(settings, field)
            if value is not None and not value.is_absolute():
                setattr(settings, field, (path.parent / value).resolve())
        return settings
