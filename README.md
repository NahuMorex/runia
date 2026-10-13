# Runia

API con bot de Telegram que arma un plan semanal de entrenamiento para running según el nivel y los objetivos del corredor. Los días con sesión, el bot pregunta la fatiga y envía la sesión del día, con alerta si el clima es desfavorable.

El detalle de los requerimientos está en [PRD.md](PRD.md).

## Requisitos

- [uv](https://docs.astral.sh/uv/) (instala Python 3.12 por su cuenta).
- Una API key de Gemini, de [Google AI Studio](https://aistudio.google.com/apikey).
- Una API key de [OpenWeatherMap](https://openweathermap.org/api) (alcanza el plan gratuito).
- Un bot de Telegram: se crea hablándole a [@BotFather](https://t.me/BotFather) con `/newbot`, que entrega el token.

## Instalación

```bash
uv sync
```

Creá un archivo `.env` en la raíz del proyecto. No se commitea.

```
GOOGLE_API_KEY=tu-api-key-de-gemini
TELEGRAM_BOT_TOKEN=el-token-de-botfather
OPENWEATHER_API_KEY=tu-api-key-de-openweathermap
JWT_SECRET=un-secreto-largo-y-aleatorio
```

Para generar el `JWT_SECRET`:

```bash
openssl rand -hex 32
```

## Levantar la app

```bash
uv run --env-file .env uvicorn app.main:app --reload
```

La API queda en `http://127.0.0.1:8000` y el bot arranca en el mismo proceso. La documentación interactiva de la API está en `http://127.0.0.1:8000/docs`.

Tres cosas a tener en cuenta:

- La app no lee el `.env` sola: hay que pasar `--env-file .env`. Sin `TELEGRAM_BOT_TOKEN` levanta la API sin el bot y lo avisa con un log `bot_disabled`.
- Tiene que haber una sola instancia corriendo por token. No uses `--workers` ni la levantes en dos terminales: los bots chocan entre sí en Telegram.
- La app tiene que estar corriendo a tu hora de envío para que llegue el mensaje del día.

La base de datos es un archivo SQLite, `runia.db`, que se crea solo al levantar. Para usar otra ruta, definí `RUNIA_DB_PATH`.

## Uso paso a paso

Los ejemplos usan `curl`; también se puede hacer todo desde `/docs`.

### 1. Crear la cuenta

La contraseña tiene entre 8 y 128 caracteres.

```bash
curl -X POST http://127.0.0.1:8000/register \
  -H "Content-Type: application/json" \
  -d '{"email": "mile@example.com", "password": "correr10km"}'
```

### 2. Iniciar sesión

```bash
curl -X POST http://127.0.0.1:8000/login \
  -H "Content-Type: application/json" \
  -d '{"email": "mile@example.com", "password": "correr10km"}'
```

La respuesta trae el `access_token`. Guardalo para los pasos siguientes:

```bash
TOKEN="pegá-acá-el-access_token"
```

El token vence a las 2 horas. Cada respuesta autenticada trae uno renovado en el header `X-Session-Token`, hasta 7 días después del login; pasado ese plazo hay que volver a iniciar sesión.

### 3. Cargar el perfil

```bash
curl -X PUT http://127.0.0.1:8000/profile \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "level": "novato",
    "frequency": 3,
    "target_km": 10,
    "city": "Rosario",
    "window_start": "07:00",
    "window_end": "09:00",
    "send_time": "06:30"
  }'
```

| Campo | Qué es | Valores |
|---|---|---|
| `level` | Nivel del corredor | `novato`, `intermedio` o `avanzado` |
| `frequency` | Días de entrenamiento por semana | Entero de 1 a 7 |
| `target_km` | Distancia objetivo | De 1 a 42.2 |
| `pace` | Ritmo actual por km | `mm:ss`, entre `3:00` y `10:00`. Obligatorio salvo para `novato` |
| `city` | Ciudad, para consultar el clima | Texto de hasta 100 caracteres, de Argentina |
| `window_start`, `window_end` | Rango horario preferido para entrenar | `HH:MM`, con al menos 1 hora de duración |
| `send_time` | Hora a la que el bot pregunta la fatiga | `HH:MM`, anterior a `window_end` |

Todos los horarios son de Buenos Aires. Para editar el perfil se usa el mismo `PUT`, enviando solo los campos que cambian. `GET /profile` devuelve los valores actuales.

### 4. Vincular el chat de Telegram

Pedí un código de vinculación, que sirve una sola vez y vence a los 15 minutos:

```bash
curl -X POST http://127.0.0.1:8000/telegram/link-code \
  -H "Authorization: Bearer $TOKEN"
```

Después abrí un chat privado con tu bot en Telegram y enviale:

```
/vincular CODIGO
```

Cada cuenta admite un único chat y el vínculo no se puede cambiar.

### 5. Generar el plan de la semana

```bash
curl -X POST http://127.0.0.1:8000/plan \
  -H "Authorization: Bearer $TOKEN"
```

Devuelve las sesiones desde hoy hasta el domingo, cada una con su fecha, tipo (`suave` o `calidad`) y bloques de caminata, trote o tramos. Si falta algún dato del perfil o el chat vinculado, responde 422 con el dato que falta.

El plan no se genera solo: hay que pedirlo una vez por semana. Si ya hay uno para la semana en curso, responde 409.

### 6. Recibir el mensaje diario

Los días con sesión planificada, a tu hora de envío:

1. El bot consulta el clima de tu ciudad para tu rango horario.
2. Te pregunta la fatiga del 1 al 10. Respondé solo con el número; si escribís otra cosa, vuelve a preguntar.
3. Te envía la sesión del día: tipo, horario sugerido, ritmo objetivo si es de calidad y la secuencia de bloques.

Además:

- Con fatiga de 8 o más, la sesión del día pasa a suave/recuperación.
- Si en tu horario hay más de 60% de probabilidad de lluvia o más de 32 °C, el mensaje incluye una alerta y el primer día alternativo de la semana sin esa condición.
- Si no respondés en 2 horas, el bot envía la sesión planificada sin ajustar por fatiga.
- Si levantás la app después de tu hora de envío, el bot pregunta igual mientras no haya terminado tu rango horario.

## Tests

```bash
uv run pytest
```

Un solo test: `uv run pytest tests/test_x.py::test_y`. Los tests no salen a la red: Gemini, OpenWeatherMap y Telegram van mockeados.

## Logs

La app escribe logs en JSON por la salida estándar. Los más útiles para seguirla:

| Evento | Qué indica |
|---|---|
| `external_error` | Falló Gemini, OpenWeatherMap o Telegram (campo `servicio`) |
| `llm_call` | Llamada a Gemini. En el mensaje diario, `resultado` dice si la redacción se usó o se descartó |
| `decision_climatica` | Si hubo alerta, por qué motivo y qué día alternativo se propuso |
| `guardrail` | Un guardarraíl de carga o el ajuste por fatiga, con el valor antes y después |
| `mensaje_diario` | Mensaje entregado, con su duración |

## Limitaciones conocidas

- No hay registro de sesiones, cronograma ni historial, así que el volumen del plan no sube de una semana a la otra y el novato no recibe sesiones de calidad.
- La alerta climática es solo informativa: no se puede reprogramar la sesión desde el bot.
- Si Gemini falla o tarda, el mensaje diario llega con un texto fijo en lugar del redactado. Si falla el clima, llega con "Clima no disponible".
- Si Telegram falla al enviar el mensaje diario, no se reintenta ese día.
- El servicio está pensado para Argentina: las ciudades se buscan dentro del país y todos los horarios son de Buenos Aires.
- No da consejo médico ni de lesiones.
