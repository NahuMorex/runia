# AGENTS.md

## Propósito
Runnia es una API con bot de Telegram que arma planes semanales de entrenamiento para running según el nivel y los objetivos del corredor.
El plan lo genera un agente con Gemini, que ajusta la sesión diaria por clima (OpenWeatherMap) y la envía por Telegram.

## Stack
- Python 3.12 (fijado con uv; no usar el 3.14 del sistema), gestor de paquetes uv
- FastAPI + uvicorn; SQLite con el módulo `sqlite3` de la stdlib
- Modelo: `gemini-3.5-flash-lite`, vía SDK oficial directo `google-genai` directo (sin frameworks de agentes)
- Bot: `python-telegram-bot` con polling, dentro del mismo proceso que la API
- Auth: hash argon2 para contraseñas, token de sesión JWT
- Tests: pytest
- Base de conocimiento: `kb.md` (todavía no existe)

## Cómo correr
- Instalar: `uv sync`
- Levantar API + bot: `uv run uvicorn app.main:app --reload`
- Tests: `uv run pytest` (uno solo: `uv run pytest tests/test_x.py::test_y`)
- Variables de entorno: `GOOGLE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `OPENWEATHER_API_KEY`, `JWT_SECRET`

## Qué NO hacer
- No inventar datos de perfil ni de plan si faltan métricas: rechazar la solicitud.
- No poner la API key en el código ni en el repo; solo se lee de `GOOGLE_API_KEY`.
- No entregar un plan sin pasar por los guardarraíles duros de carga, sobre todo para novatos.
- No dar consejo médico ni de lesiones (fuera de alcance).
- No devolver datos de otro usuario ni guardar historiales completos en el estado del agente: solo resúmenes semanales.
- No agregar features fuera del PRD