import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Cada test usa su propia base y secretos de prueba; nunca los reales."""
    monkeypatch.setenv("RUNIA_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("JWT_SECRET", "secreto-de-prueba-de-al-menos-32-bytes")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
    # Sin token de Telegram la app no levanta el bot: los tests no salen a la red.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-openweather")


@pytest.fixture
def client(env):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client):
    """Registra un usuario y devuelve los headers con su token."""

    def _auth(email="pepe@example.com"):
        credenciales = {"email": email, "password": "correr10km"}
        client.post("/register", json=credenciales)
        token = client.post("/login", json=credenciales).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _auth
