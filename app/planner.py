"""Generación del plan semanal con Gemini (RF-06a): salida JSON estructurada y timeout de 20 s.

El modelo solo propone; los guardarraíles de app/guardrails.py se aplican después.
"""

import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date
from typing import Literal

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app import config
from app.guardrails import Block, Caps, Session, WeekSummary, fastest_allowed_pace, weekly_km
from app.log import log_event
from app.profile import pace_to_seconds, seconds_to_pace

# AC-17: sin respuesta en 20 segundos, la generación se da por fallida.
GEMINI_TIMEOUT_SECONDS = 20
# El prompt solo lleva resúmenes semanales, y de pocas semanas, para no gastar tokens.
MAX_PROMPT_WEEKS = 4
# Orientación para el modelo, no un guardarraíl: quien recién empieza no trota 1 km seguido.
NOVICE_JOG_BLOCK_KM = 0.4
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

SYSTEM_INSTRUCTION = """\
Sos un entrenador de running que arma planes semanales en español.
Respondé solo con el JSON pedido, respetando estas reglas:
- Cada sesión va en un día distinto, elegido entre los días disponibles, y es "suave" o "calidad".
- Cada sesión se describe con bloques en orden: "caminata", "trote" o "tramo", cada uno con su distancia en km.
- Una sesión suave solo tiene bloques de caminata y trote, y no lleva target_pace.
- Una sesión de calidad tiene al menos un bloque "tramo" (trote al ritmo objetivo), lleva target_pace en \
formato mm:ss por km, y cada tramo lleva recovery_seconds con la recuperación que le sigue.
- Solo los km de trote y de tramos cuentan para los límites; la caminata no.
- No superes los límites indicados ni agregues sesiones de calidad si no están permitidas.
- No des consejo médico ni de lesiones.
"""


class GenerationError(Exception):
    """Gemini falló, tardó más de 20 segundos o devolvió algo que no se puede usar."""


@dataclass(frozen=True)
class PlanRequest:
    """Lo que necesita el modelo para proponer un plan; nunca sesiones individuales pasadas."""

    level: str
    target_km: float
    pace_seconds: int | None
    days: tuple[date, ...]  # días disponibles, de la fecha de generación al domingo
    session_count: int
    caps: Caps
    quality_allowed: bool
    weeks: tuple[WeekSummary, ...] = ()  # resúmenes semanales, la más reciente primero


class BlockOut(BaseModel):
    kind: Literal["caminata", "trote", "tramo"]
    km: float
    recovery_seconds: int | None = None


class SessionOut(BaseModel):
    date: str  # YYYY-MM-DD
    kind: Literal["suave", "calidad"]
    target_pace: str | None = None  # mm:ss por km
    blocks: list[BlockOut]


class PlanOut(BaseModel):
    sessions: list[SessionOut]


def _week_line(week: WeekSummary) -> str:
    parts = [
        f"{week.planned_km:g} km de trote planificados",
        "plan parcial" if week.is_partial else "semana completa",
        f"{week.completed} sesiones completadas",
    ]
    if week.hard_cut:
        parts.append("alguna sesión cortada con esfuerzo alto")
    label = f"Semana del {week.week_start.isoformat()}" if week.week_start else "Semana"
    return f"- {label}: " + ", ".join(parts)


def build_prompt(request: PlanRequest) -> str:
    """Arma el prompt con el perfil, los límites y solo resúmenes semanales."""
    pace = request.pace_seconds
    days = ", ".join(f"{d.isoformat()} ({WEEKDAYS[d.weekday()]})" for d in request.days)
    if request.quality_allowed and pace is not None:
        quality = (
            "como máximo una, con ritmo objetivo entre "
            f"{seconds_to_pace(fastest_allowed_pace(pace))} y {seconds_to_pace(pace)} min/km"
        )
    else:
        quality = "ninguna, todas las sesiones son suaves"
    lines = [
        "Datos del corredor:",
        f"- Nivel: {request.level}",
        f"- Distancia objetivo: {request.target_km:g} km",
        f"- Ritmo actual: {seconds_to_pace(pace) + ' min/km' if pace is not None else 'sin cargar'}",
        "",
        "Semana a planificar:",
        f"- Días disponibles: {days}",
        f"- Cantidad de sesiones: {request.session_count}, cada una en un día distinto",
        "",
        "Límites:",
        f"- Máximo {request.caps.session_km:g} km de trote por sesión",
        f"- Máximo {request.caps.weekly_km:g} km de trote en toda la semana",
        f"- Sesiones de calidad: {quality}",
    ]
    if request.level == "novato":
        lines.append(
            f"- Cada bloque de trote es corto, de {NOVICE_JOG_BLOCK_KM:g} km como máximo, "
            "separado del siguiente por un bloque de caminata"
        )
    if request.level == "novato" and not request.weeks:
        # AC-14
        lines.append(
            "- Es su primera semana: cada sesión alterna bloques de caminata y de trote, "
            "con la distancia en km de cada bloque"
        )
    lines += ["", "Resumen de las últimas semanas (la más reciente primero):"]
    weeks = request.weeks[:MAX_PROMPT_WEEKS]
    lines += [_week_line(w) for w in weeks] if weeks else ["- Sin semanas anteriores"]
    return "\n".join(lines)


def _call_gemini(prompt: str) -> str | None:
    client = genai.Client(
        api_key=config.google_api_key(),
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_SECONDS * 1000),
    )
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=PlanOut,
        ),
    )
    return response.text


def parse_plan(text: str | None) -> list[Session]:
    """Convierte el JSON del modelo en sesiones; falla si no respeta el formato."""
    try:
        plan = PlanOut.model_validate_json(text or "")
        return [
            Session(
                day=date.fromisoformat(s.date),
                kind=s.kind,
                blocks=tuple(Block(b.kind, b.km, b.recovery_seconds) for b in s.blocks),
                pace_seconds=pace_to_seconds(s.target_pace) if s.target_pace else None,
            )
            for s in plan.sessions
        ]
    except (ValidationError, ValueError) as error:
        raise GenerationError("Respuesta del modelo con formato inválido") from error


def _fail(motivo: str, user_id: int, error: Exception | None = None) -> GenerationError:
    log_event(
        "external_error",
        level=logging.ERROR,
        servicio="gemini",
        motivo=motivo,
        tipo=type(error).__name__ if error else None,
        codigo=getattr(error, "code", None),
        user_id=user_id,
    )
    return GenerationError(motivo)


def generate_plan(request: PlanRequest, user_id: int) -> list[Session]:
    """Pide el plan a Gemini y lo devuelve sin guardarraíles; lanza GenerationError si falla."""
    prompt = build_prompt(request)
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        text = executor.submit(_call_gemini, prompt).result(timeout=GEMINI_TIMEOUT_SECONDS)
    except FutureTimeout as error:
        raise _fail("timeout", user_id, error) from error
    except errors.APIError as error:
        raise _fail("error_de_api", user_id, error) from error
    except Exception as error:  # red caída, timeout HTTP, etc.: todo es un fallo del servicio
        raise _fail("error_de_conexion", user_id, error) from error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    try:
        sessions = parse_plan(text)
    except GenerationError as error:
        raise _fail("respuesta_invalida", user_id, error) from error
    # RNF-02: resumen del prompt y de la respuesta, no el historial completo.
    log_event(
        "llm_call",
        modelo=config.GEMINI_MODEL,
        user_id=user_id,
        duracion_ms=round((time.monotonic() - started) * 1000),
        prompt={
            "nivel": request.level,
            "sesiones_pedidas": request.session_count,
            "tope_sesion_km": request.caps.session_km,
            "tope_semanal_km": request.caps.weekly_km,
            "calidad_permitida": request.quality_allowed,
            "semanas_resumidas": min(len(request.weeks), MAX_PROMPT_WEEKS),
        },
        respuesta={
            "sesiones": len(sessions),
            "km_de_trote": weekly_km(sessions),
            "sesiones_de_calidad": sum(s.kind == "calidad" for s in sessions),
        },
    )
    return sessions
