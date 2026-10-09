"""Sesión del día y mensaje diario (RF-08, RF-16): ajuste por fatiga y texto para Telegram.

Son funciones puras. Acá se arma el mensaje con una plantilla, con los datos del plan
guardado, que ya pasó por los guardarraíles; la redacción final con Gemini está en
app/writer.py, que usa esta plantilla como fuente de datos y como respaldo.
"""

from dataclasses import dataclass
from datetime import date

from app.climate import HEAT, RAIN, Alert
from app.guardrails import EASY, INTERVAL, JOG, QUALITY, WALK, Block, Session, interval_seconds, to_easy
from app.planner import WEEKDAYS
from app.profile import seconds_to_pace
from app.weather import WindowForecast

# RF-16: con esta fatiga o más, la sesión del día es suave/recuperación.
HIGH_FATIGUE = 8
# AC-27
MAX_WORDS = 150

RECOVERY_LABEL = "suave/recuperación"
KIND_LABELS = {EASY: "suave", QUALITY: "calidad"}
BLOCK_LABELS = {WALK: "caminata", JOG: "trote", INTERVAL: "a ritmo objetivo"}
BLOCK_PLURALS = {WALK: "de caminata", JOG: "de trote", INTERVAL: "a ritmo objetivo"}
# AC-13
WEATHER_UNAVAILABLE_TEXT = "Clima no disponible: seguí las pautas base de ritmo e hidratación."
# AC-24
NO_ALTERNATIVE_TEXT = "No hay una alternativa disponible esta semana"


@dataclass(frozen=True)
class DailyMessage:
    """Mensaje diario armado con la plantilla y los datos que toda redacción debe conservar."""

    text: str
    required: tuple[str, ...]  # fragmentos obligatorios (AC-13, AC-23, AC-24, AC-28)


def is_high_fatigue(fatigue: int | None) -> bool:
    """`fatigue` es None cuando el usuario no respondió la pregunta (AC-47)."""
    return fatigue is not None and fatigue >= HIGH_FATIGUE


def adjust_for_fatigue(session: Session, fatigue: int | None) -> Session:
    """RF-16 (AC-48): con fatiga >= 8 la sesión de calidad pasa a suave, con los mismos km de trote.

    Una sesión que ya es suave queda igual.
    """
    if is_high_fatigue(fatigue) and session.kind == QUALITY:
        return to_easy(session)
    return session


def word_count(text: str) -> int:
    return len(text.split())


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _block_text(block: Block, pace_seconds: int | None) -> str:
    text = f"{_number(block.km)} km {BLOCK_LABELS[block.kind]}"
    if block.kind != INTERVAL:
        return text
    details = []
    if pace_seconds is not None:
        details.append(f"en {seconds_to_pace(interval_seconds(block.km, pace_seconds))}")
    if block.recovery_seconds:
        details.append(f"recuperación {block.recovery_seconds} s")
    return f"{text} ({', '.join(details)})" if details else text


def _sequence(items: list[str]) -> str:
    """Une los bloques en orden, agrupando las repeticiones: "3 x (a + b)"."""
    parts, i = [], 0
    while i < len(items):
        best_period, best_repeats = 1, 1
        for period in range(1, (len(items) - i) // 2 + 1):
            pattern, repeats = items[i : i + period], 1
            while items[i + repeats * period : i + (repeats + 1) * period] == pattern:
                repeats += 1
            if repeats > 1 and repeats * period > best_repeats * best_period:
                best_period, best_repeats = period, repeats
        pattern = items[i : i + best_period]
        if best_repeats == 1:
            parts.append(pattern[0])
        elif best_period == 1:
            parts.append(f"{best_repeats} x {pattern[0]}")
        else:
            parts.append(f"{best_repeats} x ({' + '.join(pattern)})")
        i += best_period * best_repeats
    return ", ".join(parts)


def _summary(session: Session) -> str:
    """Resumen de los bloques por tipo, para una secuencia que no entra en el mensaje."""
    parts = []
    for kind in dict.fromkeys(b.kind for b in session.blocks):
        blocks = [b for b in session.blocks if b.kind == kind]
        km = round(sum(b.km for b in blocks), 2)
        noun = "bloque" if len(blocks) == 1 else "bloques"
        parts.append(f"{len(blocks)} {noun} {BLOCK_PLURALS[kind]} ({_number(km)} km en total)")
    return ", ".join(parts) + ", intercalados en el orden de tu plan"


def _day_text(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day}/{day.month}"


def _reasons_text(reasons: tuple[str, ...], forecast: WindowForecast) -> str:
    texts = {
        RAIN: f"probabilidad de lluvia de {forecast.rain_percent}%",
        HEAT: f"temperatura de hasta {_number(round(forecast.temp_c, 1))} °C",
    }
    return " y ".join(texts[reason] for reason in reasons)


def _weather_parts(alert: Alert | None, weather_available: bool) -> tuple[list[str], list[str]]:
    """Líneas de clima del mensaje y los fragmentos obligatorios que contienen."""
    if not weather_available:
        return [WEATHER_UNAVAILABLE_TEXT], [WEATHER_UNAVAILABLE_TEXT]
    if alert is None:
        return [], []
    # AC-23: la alerta lleva el motivo climático específico.
    reasons = _reasons_text(alert.reasons, alert.forecast)
    if alert.alternative is None:
        return [f"Alerta climática: {reasons} en tu horario.", f"{NO_ALTERNATIVE_TEXT}."], [
            reasons,
            NO_ALTERNATIVE_TEXT,
        ]
    # Solo se informa: reprogramar la sesión (RF-07b) está fuera de alcance.
    day = _day_text(alert.alternative)
    return [
        f"Alerta climática: {reasons} en tu horario.",
        f"Día alternativo sin condición desfavorable: {day}.",
    ], [reasons, day]


def build_daily(
    planned: Session,
    window_start: str,
    window_end: str,
    fatigue: int | None = None,
    alert: Alert | None = None,
    weather_available: bool = True,
) -> DailyMessage:
    """Mensaje diario con la sesión del día, de 150 palabras o menos (AC-27, AC-28).

    `planned` es la sesión del plan; el ajuste por fatiga se aplica acá. `alert` es el
    resultado de la evaluación climática, y `weather_available` es False cuando no se
    pudo consultar el clima (AC-13).
    """
    session = adjust_for_fatigue(planned, fatigue)
    tired = is_high_fatigue(fatigue)
    kind = RECOVERY_LABEL if tired else KIND_LABELS[session.kind]
    head, required = [f"Sesión de hoy: {kind}."], [kind, window_start, window_end]
    if tired and planned.kind == QUALITY:
        head.append(f"Por tu fatiga ({fatigue}/10), la sesión de calidad pasa a suave.")
    elif tired:
        head.append(f"Por tu fatiga ({fatigue}/10), la sesión se mantiene suave.")
    head.append(f"Horario sugerido: {window_start} a {window_end}.")
    if session.kind == QUALITY and session.pace_seconds is not None:
        pace = seconds_to_pace(session.pace_seconds)
        head.append(f"Ritmo objetivo: {pace} min/km.")
        required.append(pace)
    tail, weather_required = _weather_parts(alert, weather_available)

    def message(sequence: str) -> DailyMessage:
        text = "\n".join([*head, f"Secuencia: {sequence}.", *tail])
        return DailyMessage(text, (*required, sequence, *weather_required))

    daily = message(_sequence([_block_text(b, session.pace_seconds) for b in session.blocks]))
    if word_count(daily.text) > MAX_WORDS:
        daily = message(_summary(session))
    return daily


def build_message(
    planned: Session,
    window_start: str,
    window_end: str,
    fatigue: int | None = None,
    alert: Alert | None = None,
    weather_available: bool = True,
) -> str:
    """Texto de la plantilla del mensaje diario; ver build_daily."""
    return build_daily(planned, window_start, window_end, fatigue, alert, weather_available).text
