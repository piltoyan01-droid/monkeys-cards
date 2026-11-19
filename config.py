"""Server configuration compatible with the existing BOT_TOKEN environment."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
DEFAULT_USERNAME = "monkeysmafcardsbot"


class ConfigurationError(ValueError):
    """Only fixed messages; never include credentials or API exceptions."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    username: str


def load_settings() -> Settings:
    # Preserve the current service's credential source. A stale local .env must
    # not redirect this service to the test bot or change the expected username.
    if "BOT_TOKEN" in os.environ:
        token = os.environ["BOT_TOKEN"].strip()
        username = os.environ.get("BOT_USERNAME") or DEFAULT_USERNAME
    else:
        values = dotenv_values(ROOT / ".env", interpolate=False)
        token = (values.get("BOT_TOKEN") or "").strip()
        username = (os.environ.get("BOT_USERNAME") or values.get("BOT_USERNAME")
                    or DEFAULT_USERNAME)
    username = username.strip().removeprefix("@")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        raise ConfigurationError("Не найден корректный BOT_TOKEN. Сохраните переменную окружения существующего процесса.")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ConfigurationError("Проверьте BOT_USERNAME: username бота без @.")
    return Settings(token=token, username=username)
