from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    mode: str = "model"
    base_url: str = "https://api.deepseek.com"
    model_id: str = "deepseek-flash"
    api_key: str = ""
    timeout: float = 25.0
    json_mode: bool = True
    thinking: str = "disabled"
    db_path: str = str(Path(__file__).resolve().parents[1] / "data" / "cafe.sqlite3")

    @classmethod
    def from_env(cls) -> "Settings":
        # Same behavior for uvicorn and python -m cafe; explicit process settings win.
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        settings = cls(
            mode=os.getenv("NPC_MODE", "model"),
            base_url=os.getenv("MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model_id=os.getenv("MODEL_ID", "deepseek-flash"),
            api_key=os.getenv("MODEL_API_KEY") or os.getenv("DEEPSEEK_API_KEY", ""),
            timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "25")),
            json_mode=os.getenv("MODEL_JSON_MODE", "true").lower() == "true",
            thinking=os.getenv("MODEL_THINKING", "disabled"),
            db_path=os.getenv("CAFE_DB_PATH", cls.db_path),
        )
        if settings.mode not in {"model", "rule"}:
            raise ValueError("NPC_MODE 必须为 model 或 rule")
        if settings.timeout <= 0:
            raise ValueError("MODEL_TIMEOUT_SECONDS 必须大于零")
        if settings.thinking not in {"disabled", "enabled"}:
            raise ValueError("MODEL_THINKING 必须为 disabled 或 enabled")
        return settings
