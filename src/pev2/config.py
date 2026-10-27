"""Configuration from the environment, `python-dotenv` style. The clock is pinned for the simulation."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

FIXTURES = ROOT / "fixtures"


def db_path() -> Path:
    raw = os.getenv("PEV2_DB", "data/runtime/pev2.db")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def now() -> datetime:
    """The simulation clock (PEV2_CLOCK) or the real time, always UTC."""
    raw = os.getenv("PEV2_CLOCK")
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def today() -> date:
    return now().date()


def payment_run_weekday() -> str:
    return os.getenv("PEV2_PAYMENT_RUN_WEEKDAY", "thursday")


def retry_sleep() -> float:
    return float(os.getenv("PEV2_RETRY_SLEEP", "0"))


def llm_provider() -> str:
    return os.getenv("PEV2_LLM_PROVIDER", "fixture")


def llm_settings() -> dict[str, str | None]:
    return {
        "base_url": os.getenv("PEV2_LLM_BASE_URL"),
        "api_key": os.getenv("PEV2_LLM_API_KEY"),
        "model": os.getenv("PEV2_LLM_MODEL", "gpt-4o-mini"),
        "temperature": os.getenv("PEV2_LLM_TEMPERATURE"),
    }


def studio_url() -> str:
    """The Agent Architecture Studio that receives the gap report (the loopback)."""
    return os.environ.get("PEV2_STUDIO_URL", "http://localhost:8000").rstrip("/")


def studio_user() -> str:
    return os.environ.get("PEV2_STUDIO_USER", "local")  # the studio's default identity
