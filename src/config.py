"""Configuration loading with YAML + environment variable support."""

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    api_id: int
    api_hash: str
    phone: str
    bot_token: str
    delivery_chat_id: int
    username: str  # Your Telegram username (without @) for mention detection


class LLMConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"


class EmbeddingsConfig(BaseModel):
    model: str = "voyage-3-lite"


class DigestConfig(BaseModel):
    schedule: str = "07:00"
    timezone: str = "Europe/Zurich"
    lookback_hours: int = 24
    target_length: int = 2000


class QuietHoursConfig(BaseModel):
    enabled: bool = False
    start: str = "22:00"
    end: str = "08:00"


class AnswerSuggesterConfig(BaseModel):
    enabled: bool = True
    similarity_threshold: float = 0.85
    cooldown_minutes: int = 30
    suppress_while_typing: bool = True
    show_top_matches: int = 3


class QuestionDetectionConfig(BaseModel):
    batch_size: int = 50
    max_wait_minutes: int = 10


class DataRetentionConfig(BaseModel):
    messages_days: int = 90
    cleanup_schedule: str = "03:00"


class ChatPriority(BaseModel):
    chat_id: int
    priority: int = 3


class ChatsConfig(BaseModel):
    default_priority: int = 3
    priorities: list[ChatPriority] = Field(default_factory=list)
    ignore_patterns: list[str] = Field(default_factory=list)  # Chat names to ignore (supports wildcards)


class DashboardConfig(BaseModel):
    enabled: bool = True
    port: int = 8000


class Config(BaseModel):
    telegram: TelegramConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    answer_suggester: AnswerSuggesterConfig = Field(default_factory=AnswerSuggesterConfig)
    question_detection: QuestionDetectionConfig = Field(default_factory=QuestionDetectionConfig)
    data_retention: DataRetentionConfig = Field(default_factory=DataRetentionConfig)
    chats: ChatsConfig = Field(default_factory=ChatsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)


# Global config instance
_config: Optional[Config] = None


def _substitute_env_vars(value: str) -> str:
    """Replace ${VAR} patterns with environment variable values."""
    pattern = r'\$\{([^}]+)\}'

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.getenv(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable {var_name} not set")
        return env_value

    return re.sub(pattern, replacer, value)


def _process_config_values(obj):
    """Recursively process config values, substituting environment variables."""
    if isinstance(obj, dict):
        return {k: _process_config_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_process_config_values(item) for item in obj]
    elif isinstance(obj, str):
        return _substitute_env_vars(obj)
    return obj


def load_config(config_path: Optional[Path] = None, env_path: Optional[Path] = None) -> Config:
    """Load configuration from YAML file and environment variables."""
    global _config

    # Determine paths
    base_dir = Path(__file__).parent.parent
    if env_path is None:
        env_path = base_dir / ".env"
    if config_path is None:
        # Prefer config in data dir (written by setup in Docker), fall back to project root
        data_config = base_dir / "data" / "config.yaml"
        config_path = data_config if data_config.exists() else base_dir / "config.yaml"

    # Load environment variables
    if env_path.exists():
        load_dotenv(env_path)

    # Load and parse YAML
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    # Substitute environment variables
    processed_config = _process_config_values(raw_config)

    # Validate with Pydantic
    _config = Config(**processed_config)
    return _config


def get_config() -> Config:
    """Get the global config instance. Must call load_config first."""
    if _config is None:
        raise RuntimeError("Config not loaded. Call load_config() first.")
    return _config


def get_data_dir() -> Path:
    """Get the data directory path."""
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    return data_dir


def get_db_path() -> Path:
    """Get the SQLite database path."""
    return get_data_dir() / "messages.db"


def get_chroma_path() -> Path:
    """Get the ChromaDB directory path."""
    chroma_dir = get_data_dir() / "chroma"
    chroma_dir.mkdir(mode=0o700, exist_ok=True)
    return chroma_dir


def get_lock_path() -> Path:
    """Get the lock file path."""
    return get_data_dir() / ".lock"


def get_session_path() -> Path:
    """Get the Telethon session file path."""
    return get_data_dir() / "telegram.session"
