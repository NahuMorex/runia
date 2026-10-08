import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Cada test usa su propia base y secretos de prueba; nunca los reales."""
    monkeypatch.setenv("RUNNIA_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("JWT_SECRET", "secreto-de-prueba-de-al-menos-32-bytes")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-telegram")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-openweather")
