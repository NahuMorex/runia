"""Redacción del mensaje diario con Gemini (RF-08, RNF-01).

El modelo solo redacta: los datos salen de la plantilla de app/daily.py. Si Gemini
falla, tarda o devuelve un texto que no conserva los datos, se envía la plantilla.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from google import genai
from google.genai import errors, types

from app import config
from app.daily import MAX_WORDS, DailyMessage, word_count
from app.log import log_event

# RNF-01: la respuesta de fatiga, Gemini y el envío entran en 10 segundos.
MESSAGE_TIMEOUT_SECONDS = 5
# Margen para que el modelo no quede al borde de las 150 palabras de AC-27.
PROMPT_MAX_WORDS = 110

SYSTEM_INSTRUCTION = f"""\
Sos un entrenador de running que le escribe por Telegram a un corredor, en español rioplatense.
Te paso los datos de la sesión de hoy y redactás el mensaje, respetando estas reglas:
- Usá solo los datos que recibís. No agregues números, distancias, horarios, ritmos ni días.
- Copiá sin cambiar los números, los horarios, la secuencia de bloques, los motivos de la \
alerta climática y el día alternativo, tal como están escritos.
- Si los datos dicen "Clima no disponible: seguí las pautas base de ritmo e hidratación." o \
"No hay una alternativa disponible esta semana", copiá esa frase textual.
- No hagas preguntas ni ofrezcas reprogramar la sesión.
- No des consejo médico ni de lesiones.
- Texto plano, sin markdown ni asteriscos, de {PROMPT_MAX_WORDS} palabras como máximo.
"""

_NUMBER_RE = re.compile(r"\d+(?:[.,:/]\d+)*")
_MARKDOWN = ("*", "#", "`")


def build_prompt(daily: DailyMessage) -> str:
    return f"Datos de la sesión de hoy:\n{daily.text}"


def rejection_reason(text: str | None, daily: DailyMessage) -> str | None:
    """Motivo por el que la redacción del modelo no se puede enviar, o None si sirve."""
    text = (text or "").strip()
    if not text:
        return "vacio"
    if word_count(text) > MAX_WORDS:  # AC-27
        return "supera_150_palabras"
    lowered = text.casefold()
    if any(fragment.casefold() not in lowered for fragment in daily.required):
        return "falta_un_dato"  # AC-13, AC-23, AC-24, AC-28
    if not set(_NUMBER_RE.findall(text)) <= set(_NUMBER_RE.findall(daily.text)):
        return "numero_inventado"
    if "?" in text:
        return "pide_confirmacion"  # reprogramar (RF-07b) está fuera de alcance
    if any(mark in text for mark in _MARKDOWN):
        return "formato"
    return None


def _call_gemini(prompt: str) -> str | None:
    client = genai.Client(
        api_key=config.google_api_key(),
        http_options=types.HttpOptions(timeout=MESSAGE_TIMEOUT_SECONDS * 1000),
    )
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
    )
    return response.text


def _external_error(motivo: str, user_id: int, error: Exception) -> None:
    log_event(
        "external_error",
        level=logging.ERROR,
        servicio="gemini",
        motivo=motivo,
        tipo=type(error).__name__,
        codigo=getattr(error, "code", None),
        user_id=user_id,
    )


def write_message(daily: DailyMessage, user_id: int) -> str:
    """Texto a enviar: la redacción de Gemini si conserva los datos, o la plantilla si no.

    Nunca falla: el mensaje diario se entrega igual aunque Gemini no esté disponible.
    """
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        text = executor.submit(_call_gemini, build_prompt(daily)).result(
            timeout=MESSAGE_TIMEOUT_SECONDS
        )
    except FutureTimeout as error:
        _external_error("timeout", user_id, error)
        return daily.text
    except errors.APIError as error:
        _external_error("error_de_api", user_id, error)
        return daily.text
    except Exception as error:  # red caída, timeout HTTP, etc.: todo es un fallo del servicio
        _external_error("error_de_conexion", user_id, error)
        return daily.text
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    rejection = rejection_reason(text, daily)
    # RNF-02: resumen del prompt y de la respuesta, no los textos completos.
    log_event(
        "llm_call",
        modelo=config.GEMINI_MODEL,
        user_id=user_id,
        uso="mensaje_diario",
        duracion_ms=round((time.monotonic() - started) * 1000),
        prompt={"palabras": word_count(daily.text), "datos_obligatorios": len(daily.required)},
        respuesta={
            "palabras": word_count(text or ""),
            "resultado": "descartada" if rejection else "usada",
            "motivo": rejection,
        },
    )
    return daily.text if rejection else text.strip()
