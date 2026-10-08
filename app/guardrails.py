"""Guardarraíles duros de carga del plan semanal (RF-06b, RF-06d a RF-06i).

Son funciones puras, sin LLM ni base de datos: reciben las sesiones propuestas y
devuelven las sesiones corregidas junto con los ajustes aplicados.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from app.log import log_event

# RF-06g: tope inicial por sesión, en km de trote.
INITIAL_SESSION_KM = {"novato": 2.0, "intermedio": 5.0, "avanzado": 10.0}
# RF-06b
MAX_WEEKLY_INCREASE_PERCENT = 10
# AC-22: el ritmo objetivo puede ser hasta un 5,6% más rápido que el ritmo actual.
MAX_PACE_REDUCTION_PERMILLE = 56
# RF-06d
NOVICE_QUALITY_WEEKS = 3

EASY = "suave"
QUALITY = "calidad"


class InvalidPlan(ValueError):
    """El plan propuesto no se puede corregir sin inventar datos."""


@dataclass(frozen=True)
class Session:
    day: date
    kind: str  # 'suave' o 'calidad'
    jog_km: float  # solo km de trote; la caminata no cuenta (AC-18)
    pace_seconds: int | None = None  # ritmo objetivo por km, solo en sesiones de calidad


@dataclass(frozen=True)
class WeekSummary:
    """Resumen de una semana con plan; nunca sesiones individuales."""

    planned_km: float  # volumen planificado, tal como se generó
    is_partial: bool = False
    completed: int = 0  # sesiones Completadas
    hard_cut: bool = False  # alguna sesión Parcial/Cortada con RPE >= 7


@dataclass(frozen=True)
class Caps:
    session_km: float
    session_rule: str
    weekly_km: float
    weekly_rule: str


@dataclass(frozen=True)
class Adjustment:
    """Un guardarraíl aplicado, con el valor antes y después (RNF-02)."""

    rule: str
    before: float | int | str
    after: float | int | str
    day: date | None = None  # None cuando el ajuste es sobre la semana entera


# Las distancias se comparan en centésimas de km enteras para no depender del redondeo de floats.
def _units(km: float) -> int:
    return round(km * 100)


def _km(units: int) -> float:
    return units / 100


def weekly_km(sessions: Sequence[Session]) -> float:
    """Volumen semanal: suma de los km de trote."""
    return _km(sum(_units(s.jog_km) for s in sessions))


def check_structure(
    sessions: Sequence[Session], expected: int, first_day: date, last_day: date
) -> None:
    """Falla si el plan no tiene exactamente las sesiones esperadas, en días distintos (AC-65)."""
    if len(sessions) != expected:
        raise InvalidPlan(f"El plan tiene {len(sessions)} sesiones y se esperaban {expected}")
    if len({s.day for s in sessions}) != len(sessions):
        raise InvalidPlan("El plan tiene más de una sesión en el mismo día")
    for s in sessions:
        if not first_day <= s.day <= last_day:
            raise InvalidPlan(f"La sesión del {s.day} está fuera de la semana del plan")
        if s.kind not in (EASY, QUALITY):
            raise InvalidPlan(f"Tipo de sesión desconocido: {s.kind}")
        if not math.isfinite(s.jog_km) or s.jog_km < 0:
            raise InvalidPlan(f"Km de trote inválidos en la sesión del {s.day}")
        if s.kind == QUALITY and s.pace_seconds is None:
            raise InvalidPlan(f"La sesión de calidad del {s.day} no tiene ritmo objetivo")


def novice_quality_allowed(pace_seconds: int | None, recent_weeks: Sequence[WeekSummary]) -> bool:
    """RF-06d: el novato solo recibe calidad con ritmo cargado y 3 semanas cerradas buenas.

    `recent_weeks` son las últimas 3 semanas cerradas que tuvieron plan; una semana
    sin plan no tiene sesiones Completadas, así que con menos de 3 no se cumple.
    """
    return (
        pace_seconds is not None
        and len(recent_weeks) >= NOVICE_QUALITY_WEEKS
        and all(w.completed >= 1 and not w.hard_cut for w in recent_weeks[:NOVICE_QUALITY_WEEKS])
    )


def remove_quality(sessions: Sequence[Session]) -> tuple[list[Session], list[Adjustment]]:
    """AC-20: pasa a suave todas las sesiones de calidad."""
    return _to_easy(sessions, keep=0, rule="novato_sin_calidad")


def limit_quality(sessions: Sequence[Session]) -> tuple[list[Session], list[Adjustment]]:
    """AC-21: deja como máximo una sesión de calidad, la primera de la semana."""
    return _to_easy(sessions, keep=1, rule="una_sesion_de_calidad")


def _to_easy(
    sessions: Sequence[Session], keep: int, rule: str
) -> tuple[list[Session], list[Adjustment]]:
    kept = sorted(s.day for s in sessions if s.kind == QUALITY)[:keep]
    result, adjustments = [], []
    for s in sessions:
        if s.kind == QUALITY and s.day not in kept:
            adjustments.append(Adjustment(rule, QUALITY, EASY, s.day))
            s = replace(s, kind=EASY, pace_seconds=None)
        result.append(s)
    return result, adjustments


def fastest_allowed_pace(pace_seconds: int) -> int:
    """AC-22: el ritmo actual reducido un 5,6%, redondeado hacia el lado lento."""
    return -(-pace_seconds * (1000 - MAX_PACE_REDUCTION_PERMILLE) // 1000)


def clamp_quality_pace(
    sessions: Sequence[Session], pace_seconds: int
) -> tuple[list[Session], list[Adjustment]]:
    """AC-22: lleva el ritmo objetivo de la calidad al rango permitido."""
    fastest = fastest_allowed_pace(pace_seconds)
    result, adjustments = [], []
    for s in sessions:
        if s.kind == QUALITY:
            if s.pace_seconds is None:
                raise InvalidPlan(f"La sesión de calidad del {s.day} no tiene ritmo objetivo")
            clamped = min(max(s.pace_seconds, fastest), pace_seconds)
            if clamped != s.pace_seconds:
                adjustments.append(Adjustment("ritmo_objetivo", s.pace_seconds, clamped, s.day))
                s = replace(s, pace_seconds=clamped)
        result.append(s)
    return result, adjustments


def load_caps(
    level: str,
    frequency: int,
    target_km: float,
    previous_week: WeekSummary | None = None,
    last_full_week_km: float | None = None,
) -> Caps:
    """Topes de km de trote por sesión y por semana que le corresponden al plan.

    `previous_week` es la semana inmediatamente anterior (None si no tuvo plan) y
    `last_full_week_km` el volumen planificado de la última semana completa del usuario.
    """
    initial = INITIAL_SESSION_KM[level]
    if previous_week is None or (previous_week.is_partial and last_full_week_km is None):
        # RF-06g (AC-61, AC-62): tope inicial por sesión y por semana.
        if initial <= target_km:
            session_km, session_rule = initial, "tope_inicial_sesion"
        else:
            session_km, session_rule = target_km, "distancia_objetivo"
        return Caps(session_km, session_rule, _km(_units(initial) * frequency), "tope_inicial_semanal")

    # RF-06b: si la semana anterior fue parcial, la base es la última semana completa (AC-74).
    base = _units(last_full_week_km if previous_week.is_partial else previous_week.planned_km)
    if previous_week.completed == 0:
        weekly, weekly_rule = base, "sin_aumento"  # RF-06h (AC-63)
    else:
        weekly, weekly_rule = base * (100 + MAX_WEEKLY_INCREASE_PERCENT) // 100, "tope_10"
    # RF-06i (AC-67): después de la primera semana el único tope por sesión es la distancia objetivo.
    return Caps(target_km, "distancia_objetivo", _km(weekly), weekly_rule)


def cap_session_km(
    sessions: Sequence[Session], cap_km: float, rule: str
) -> tuple[list[Session], list[Adjustment]]:
    """Recorta cada sesión al tope por sesión (AC-61, AC-67)."""
    result, adjustments = [], []
    for s in sessions:
        if _units(s.jog_km) > _units(cap_km):
            adjustments.append(Adjustment(rule, s.jog_km, cap_km, s.day))
            s = replace(s, jog_km=cap_km)
        result.append(s)
    return result, adjustments


def cap_weekly_km(
    sessions: Sequence[Session], cap_km: float, rule: str
) -> tuple[list[Session], list[Adjustment]]:
    """Recorta el volumen semanal al tope, en proporción y sin piso mínimo (AC-18, AC-63)."""
    total, cap = sum(_units(s.jog_km) for s in sessions), _units(cap_km)
    if total <= cap:
        return list(sessions), []
    # La división entera redondea hacia abajo, así que la suma nunca supera el tope.
    result = [replace(s, jog_km=_km(_units(s.jog_km) * cap // total)) for s in sessions]
    return result, [Adjustment(rule, _km(total), weekly_km(result))]


def apply_guardrails(
    sessions: Sequence[Session],
    *,
    level: str,
    frequency: int,
    target_km: float,
    pace_seconds: int | None,
    previous_week: WeekSummary | None = None,
    last_full_week_km: float | None = None,
    recent_weeks: Sequence[WeekSummary] = (),
) -> tuple[list[Session], list[Adjustment]]:
    """Aplica todos los guardarraíles, en orden, a las sesiones propuestas por el modelo."""
    current: list[Session] = [replace(s, jog_km=_km(_units(s.jog_km))) for s in sessions]
    adjustments: list[Adjustment] = []

    def step(result: tuple[list[Session], list[Adjustment]]) -> None:
        nonlocal current
        current = result[0]
        adjustments.extend(result[1])

    if level == "novato" and not novice_quality_allowed(pace_seconds, recent_weeks):
        step(remove_quality(current))
    step(limit_quality(current))
    if any(s.kind == QUALITY for s in current):
        if pace_seconds is None:
            raise InvalidPlan("No hay ritmo actual para limitar la sesión de calidad")
        step(clamp_quality_pace(current, pace_seconds))
    caps = load_caps(level, frequency, target_km, previous_week, last_full_week_km)
    step(cap_session_km(current, caps.session_km, caps.session_rule))
    step(cap_weekly_km(current, caps.weekly_km, caps.weekly_rule))
    return current, adjustments


def log_adjustments(adjustments: Sequence[Adjustment], user_id: int) -> None:
    """RNF-02: registra cada guardarraíl aplicado con el valor antes y después."""
    for a in adjustments:
        log_event(
            "guardrail",
            regla=a.rule,
            antes=a.before,
            despues=a.after,
            fecha=a.day.isoformat() if a.day else None,
            user_id=user_id,
        )
