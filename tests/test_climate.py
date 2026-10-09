from datetime import date, timedelta

import pytest

from app.climate import (
    Alert,
    alternative_days,
    evaluate,
    first_alternative,
    is_unfavorable,
    unfavorable_reasons,
)
from app.weather import WindowForecast

LUNES = date(2026, 10, 5)
BUENO = WindowForecast(rain_percent=10, temp_c=20.0)
LLUVIA = WindowForecast(rain_percent=80, temp_c=20.0)
CALOR = WindowForecast(rain_percent=10, temp_c=35.0)


def dia(n):
    """Día de la semana de prueba: 0 es lunes y 6 es domingo."""
    return LUNES + timedelta(days=n)


def semana(*pronosticos):
    return {dia(n): p for n, p in enumerate(pronosticos)}


# --- Condición desfavorable (RF-07a, AC-23) ---


@pytest.mark.parametrize(
    "lluvia, temp, motivos",
    [
        (0, 15.0, ()),
        (60, 32.0, ()),  # los límites no son desfavorables: tiene que superarlos
        (61, 32.0, ("lluvia",)),
        (60, 32.1, ("calor",)),
        (100, 20.0, ("lluvia",)),
        (0, 40.0, ("calor",)),
        (61, 32.1, ("lluvia", "calor")),
    ],
)
def test_es_desfavorable_con_lluvia_mayor_a_60_o_temperatura_mayor_a_32(lluvia, temp, motivos):
    pronostico = WindowForecast(rain_percent=lluvia, temp_c=temp)
    assert unfavorable_reasons(pronostico) == motivos
    assert is_unfavorable(pronostico) == bool(motivos)


# --- Días candidatos a alternativa (AC-24) ---


@pytest.mark.parametrize(
    "sesion, candidatos",
    [
        (0, [1, 2, 3]),  # lunes: martes, miércoles y jueves
        (3, [4, 5, 6]),  # jueves: llega justo al domingo
        (4, [5, 6]),  # viernes: el lunes siguiente queda afuera
        (5, [6]),
        (6, []),  # domingo: no queda ningún día en la semana
    ],
)
def test_los_candidatos_son_los_3_dias_siguientes_sin_pasar_del_domingo(sesion, candidatos):
    assert alternative_days(dia(sesion)) == [dia(n) for n in candidatos]


# --- Primer día alternativo (AC-24) ---


def test_la_alternativa_es_el_primer_dia_siguiente_sin_condicion_desfavorable():
    pronosticos = semana(LLUVIA, CALOR, BUENO, BUENO)
    assert first_alternative(dia(0), pronosticos) == dia(2)


def test_la_alternativa_puede_ser_el_dia_siguiente():
    assert first_alternative(dia(0), semana(LLUVIA, BUENO, BUENO, BUENO)) == dia(1)


def test_no_busca_alternativa_mas_alla_de_los_3_dias_siguientes():
    pronosticos = semana(LLUVIA, LLUVIA, CALOR, LLUVIA, BUENO)
    assert first_alternative(dia(0), pronosticos) is None


def test_no_ofrece_como_alternativa_un_dia_de_la_semana_siguiente():
    # Sesión el sábado: el domingo es desfavorable y el lunes siguiente no cuenta.
    pronosticos = {dia(5): LLUVIA, dia(6): CALOR, dia(7): BUENO, dia(8): BUENO}
    assert first_alternative(dia(5), pronosticos) is None


def test_el_domingo_no_tiene_alternativa():
    assert first_alternative(dia(6), {dia(6): LLUVIA, dia(7): BUENO}) is None


def test_un_dia_sin_pronostico_no_se_ofrece_como_alternativa():
    pronosticos = {dia(0): LLUVIA, dia(1): None, dia(3): BUENO}  # sin dato del miércoles
    assert first_alternative(dia(0), pronosticos) == dia(3)
    assert first_alternative(dia(0), {dia(0): LLUVIA}) is None


def test_la_alternativa_no_mira_dias_anteriores_ni_el_de_la_sesion():
    pronosticos = semana(BUENO, BUENO, LLUVIA, LLUVIA, LLUVIA, LLUVIA)
    assert first_alternative(dia(2), pronosticos) is None


# --- Evaluación de la sesión del día (AC-23, AC-24, AC-64) ---


def test_sin_condicion_desfavorable_no_hay_alerta():
    assert evaluate(dia(0), semana(BUENO, LLUVIA, LLUVIA, LLUVIA)) is None


def test_la_alerta_lleva_el_motivo_los_maximos_y_la_alternativa():
    alerta = evaluate(dia(1), semana(BUENO, LLUVIA, CALOR, BUENO))
    assert alerta == Alert(reasons=("lluvia",), forecast=LLUVIA, alternative=dia(3))


def test_la_alerta_puede_tener_los_dos_motivos():
    ambos = WindowForecast(rain_percent=90, temp_c=36.0)
    alerta = evaluate(dia(0), semana(ambos, BUENO))
    assert alerta.reasons == ("lluvia", "calor") and alerta.alternative == dia(1)


# AC-64: sin día alternativo, la alerta queda sin alternativa.
def test_la_alerta_sin_dia_alternativo_disponible():
    alerta = evaluate(dia(4), semana(BUENO, BUENO, BUENO, BUENO, CALOR, LLUVIA, CALOR))
    assert alerta == Alert(reasons=("calor",), forecast=CALOR, alternative=None)


def test_evaluar_sin_el_pronostico_del_dia_de_la_sesion_falla():
    with pytest.raises(KeyError):
        evaluate(dia(0), {dia(1): BUENO})
    with pytest.raises(KeyError):
        evaluate(dia(0), {dia(0): None, dia(1): BUENO})
