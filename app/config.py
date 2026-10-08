"""Configuración leída de variables de entorno. Nunca hay secretos en el código."""

import os

DEFAULT_DB_PATH = "runnia.db"
TIMEZONE = "America/Argentina/Buenos_Aires"
GEMINI_MODEL = "gemini-3.5-flash-lite"


def require_env(name: str) -> str:
    """Devuelve la variable de entorno o falla si falta o está vacía."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable de entorno {name}")
    return value


def google_api_key() -> str:
    return require_env("GOOGLE_API_KEY")


def telegram_bot_token() -> str:
    return require_env("TELEGRAM_BOT_TOKEN")


def openweather_api_key() -> str:
    return require_env("OPENWEATHER_API_KEY")


def jwt_secret() -> str:
    return require_env("JWT_SECRET")


def db_path() -> str:
    return os.environ.get("RUNNIA_DB_PATH", DEFAULT_DB_PATH)
