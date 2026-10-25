"""Portable first-run configuration, using only temporary fake credentials."""

from pathlib import Path
import os
import shutil

import pytest

from risk_monitor.config import Settings


ROOT = Path(__file__).resolve().parent.parent
ENV_FIELDS = (
    "TODO_DIRECTORY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "APPRISE_CONFIG",
    "GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # python-dotenv writes into os.environ; keep all test credentials isolated.
    monkeypatch.setattr("risk_monitor.config.os.environ", {
        name: value for name, value in os.environ.items() if name not in ENV_FIELDS
    })


def test_downloaded_template_loads_without_personal_paths_or_secrets(tmp_path):
    shutil.copyfile(ROOT / "settings.toml", tmp_path / "settings.toml")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env")

    settings = Settings.load(tmp_path / "settings.toml")

    assert settings.todo_directory == tmp_path / "todos"
    assert settings.gmail_credentials_file == tmp_path / "credentials/gmail-client.json"
    assert settings.gmail_token_file == tmp_path / "data/gmail-token.json"
    assert not settings.llm_api_key.get_secret_value()
    assert not settings.llm_base_url
    assert not settings.telegram_bot_token.get_secret_value()
    assert not settings.telegram_chat_id
    assert not (tmp_path / "data").exists()


def test_local_env_sets_model_notification_and_paths(tmp_path):
    (tmp_path / "settings.toml").write_text('todo_directory = "todos"\n', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "LLM_BASE_URL=https://example.invalid/v1\nLLM_API_KEY=fake-key\n"
        "LLM_MODEL=test-model\nTELEGRAM_BOT_TOKEN=fake-token\nTELEGRAM_CHAT_ID=123\n"
        "TODO_DIRECTORY=personal-todos\nGMAIL_CREDENTIALS_FILE=credentials/client.json\n"
        "GMAIL_TOKEN_FILE=credentials/token.json\nAPPRISE_CONFIG=credentials/apprise.txt\n",
        encoding="utf-8",
    )

    settings = Settings.load(tmp_path / "settings.toml")

    assert settings.llm_base_url == "https://example.invalid/v1"
    assert settings.llm_api_key.get_secret_value() == "fake-key"
    assert settings.llm_model == "test-model"
    assert settings.telegram_bot_token.get_secret_value() == "fake-token"
    assert settings.telegram_chat_id == "123"
    assert settings.todo_directory == tmp_path / "personal-todos"
    assert settings.gmail_credentials_file == tmp_path / "credentials/client.json"
    assert settings.gmail_token_file == tmp_path / "credentials/token.json"
    assert settings.apprise_config == tmp_path / "credentials/apprise.txt"


def test_process_environment_preserves_existing_absolute_todo_location(tmp_path, monkeypatch):
    (tmp_path / "settings.toml").write_text('todo_directory = "todos"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("TODO_DIRECTORY=other-todos\nLLM_API_KEY=file-key\n", encoding="utf-8")
    personal = tmp_path / "existing-desktop-todos"
    monkeypatch.setenv("TODO_DIRECTORY", str(personal))
    monkeypatch.setenv("LLM_API_KEY", "process-key")

    settings = Settings.load(tmp_path / "settings.toml")

    assert settings.todo_directory == personal
    assert settings.llm_api_key.get_secret_value() == "process-key"
