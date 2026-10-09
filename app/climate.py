"""Evaluación climática de la sesión del día (RF-07a): condición desfavorable y día alternativo.

Son funciones puras, sin red ni base de datos: reciben los máximos pronosticados
de cada día, ya calculados con app/weather.py.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from app.weather import WindowForecast

# RF-07a: desfavorable si supera alguno de estos máximos (no si los iguala).
RAIN_LIMIT_PERCENT = 60
TEMP_LIMIT_C = 32
# AC-24: la alternativa se busca en los 3 días siguientes, sin pasar del domingo.
ALTERNATIVE_DAYS = 3
SUNDAY = 6

RAIN = "lluvia"
HEAT = "calor"


@dataclass(frozen=True)
class Alert:
    """Condición desfavorable para la sesión del día (AC-23) y su alternativa (AC-24)."""

    reasons: tuple[str, ...]  # 'lluvia', 'calor' o ambos
    forecast: WindowForecast  # máximos del día de la sesión
    alternative: date | None  # None si no hay alternativa disponible esta semana (AC-64)


def unfavorable_reasons(forecast: WindowForecast) -> tuple[str, ...]:
    """Motivos climáticos por los que el rango horario es desfavorable; vacío si no lo es."""
    reasons = []
    if forecast.rain_percent > RAIN_LIMIT_PERCENT:
        reasons.append(RAIN)
    if forecast.temp_c > TEMP_LIMIT_C:
        reasons.append(HEAT)
    return tuple(reasons)


def is_unfavorable(forecast: WindowForecast) -> bool:
    return bool(unfavorable_reasons(forecast))


def alternative_days(day: date) -> list[date]:
    """Los 3 días siguientes a la sesión que no pasan del domingo de esa semana."""
    sunday = day + timedelta(days=SUNDAY - day.weekday())
    candidates = (day + timedelta(days=i) for i in range(1, ALTERNATIVE_DAYS + 1))
    return [d for d in candidates if d <= sunday]


def first_alternative(day: date, forecasts: Mapping[date, WindowForecast | None]) -> date | None:
    """Primer día alternativo cuyo rango horario no tiene condición desfavorable (AC-24).

    Un día sin pronóstico no se ofrece: no se puede afirmar que no sea desfavorable.
    """
    for candidate in alternative_days(day):
        forecast = forecasts.get(candidate)
        if forecast is not None and not is_unfavorable(forecast):
            return candidate
    return None


def evaluate(day: date, forecasts: Mapping[date, WindowForecast | None]) -> Alert | None:
    """Alerta para la sesión de `day`, o None si su rango horario no es desfavorable.

    `forecasts` tiene los máximos de cada día dentro del rango horario preferido y
    debe incluir el de `day`; sin ese dato no hay evaluación posible.
    """
    forecast = forecasts[day]
    if forecast is None:
        raise KeyError(day)
    reasons = unfavorable_reasons(forecast)
    if not reasons:
        return None
    return Alert(reasons, forecast, first_alternative(day, forecasts))
