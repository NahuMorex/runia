from datetime import date, timedelta

import pytest

from app.climate import Alert
from app.daily import MAX_WORDS, adjust_for_fatigue, build_message, word_count
from app.guardrails import Block, Session
from app.weather import WindowForecast

LUNES = date(2026, 10, 5)
JUEVES = LUNES + timedelta(days=3)
RANGO = ("07:00", "09:00")
CLIMA_NO_DISPONIBLE = "Clima no disponible: seguí las pautas base de ritmo e hidratación."
SIN_ALTERNATIVA = "No hay una alternativa disponible esta semana"

SUAVE = Session(LUNES, "suave", (Block("trote", 5),))
NOVATA = Session(LUNES, "suave", (Block("caminata", 0.4), Block("trote", 0.4)) * 3)
CALIDAD = Session(
    LUNES,
    "calidad",
    (
        Block("trote", 2),
        Block("tramo", 0.4, recovery_seconds=90),
        Block("tramo", 0.4, recovery_seconds=90),
        Block("caminata", 0.5),
    ),
    pace_seconds=345,
)
LLUVIA = Alert(("lluvia",), WindowForecast(80, 20.0), alternative=JUEVES)


def lineas(texto):
    return texto.splitlines()


# --- Ajuste por fatiga (RF-16, AC-48) ---


@pytest.mark.parametrize("fatiga", [8, 9, 10])
def test_con_fatiga_de_8_o_mas_la_sesion_de_calidad_pasa_a_suave(fatiga):
    sesion = adjust_for_fatigue(CALIDAD, fatiga)
    assert sesion.kind == "suave" and sesion.pace_seconds is None
    assert all(b.kind != "tramo" for b in sesion.blocks)
    assert sesion.jog_km == CALIDAD.jog_km == 2.8
    assert sesion.day == CALIDAD.day


@pytest.mark.parametrize("fatiga", [1, 7, None])
def test_con_fatiga_menor_a_8_o_sin_respuesta_la_sesion_no_cambia(fatiga):
    assert adjust_for_fatigue(CALIDAD, fatiga) == CALIDAD
    assert adjust_for_fatigue(SUAVE, fatiga) == SUAVE


def test_una_sesion_que_ya_es_suave_queda_igual_con_fatiga_alta():
    assert adjust_for_fatigue(NOVATA, 10) == NOVATA


# --- Mensaje diario (AC-28) ---


def test_el_mensaje_de_una_sesion_suave_tiene_tipo_horario_y_secuencia():
    assert lineas(build_message(SUAVE, *RANGO, fatigue=3)) == [
        "Sesión de hoy: suave.",
        "Horario sugerido: 07:00 a 09:00.",
        "Secuencia: 5 km trote.",
    ]


def test_el_mensaje_de_una_sesion_de_calidad_tiene_ritmo_objetivo_y_tramos():
    assert lineas(build_message(CALIDAD, *RANGO, fatigue=5)) == [
        "Sesión de hoy: calidad.",
        "Horario sugerido: 07:00 a 09:00.",
        "Ritmo objetivo: 5:45 min/km.",
        "Secuencia: 2 km trote, 2 x 0,4 km a ritmo objetivo (en 2:18, recuperación 90 s), "
        "0,5 km caminata.",
    ]


def test_el_mensaje_de_una_novata_tiene_la_secuencia_de_caminata_y_trote():
    texto = build_message(NOVATA, "18:30", "20:00", fatigue=2)
    assert "Horario sugerido: 18:30 a 20:00." in texto
    assert "Secuencia: 3 x (0,4 km caminata + 0,4 km trote)." in texto
    assert "Ritmo objetivo" not in texto


def test_la_secuencia_respeta_el_orden_de_bloques_que_no_se_repiten():
    sesion = Session(
        LUNES, "suave", (Block("caminata", 0.5), Block("trote", 1), Block("caminata", 0.3), Block("trote", 1.25))
    )
    texto = build_message(sesion, *RANGO)
    assert "Secuencia: 0,5 km caminata, 1 km trote, 0,3 km caminata, 1,25 km trote." in texto


# --- Fatiga en el mensaje (RF-16, AC-47) ---


def test_con_fatiga_alta_el_mensaje_de_una_sesion_de_calidad_pasa_a_suave_recuperacion():
    texto = build_message(CALIDAD, *RANGO, fatigue=8)
    assert lineas(texto)[:2] == [
        "Sesión de hoy: suave/recuperación.",
        "Por tu fatiga (8/10), la sesión de calidad pasa a suave.",
    ]
    assert "Secuencia: 2,8 km trote, 0,5 km caminata." in texto
    assert "Ritmo objetivo" not in texto and "ritmo objetivo" not in texto


def test_con_fatiga_alta_una_sesion_suave_se_mantiene_y_el_mensaje_lo_dice():
    texto = build_message(SUAVE, *RANGO, fatigue=9)
    assert lineas(texto)[:2] == [
        "Sesión de hoy: suave/recuperación.",
        "Por tu fatiga (9/10), la sesión se mantiene suave.",
    ]
    assert "Secuencia: 5 km trote." in texto


# AC-47: sin respuesta de fatiga, el mensaje lleva la sesión planificada sin ajustar.
def test_sin_respuesta_de_fatiga_el_mensaje_lleva_la_sesion_planificada():
    texto = build_message(CALIDAD, *RANGO, fatigue=None)
    assert "Sesión de hoy: calidad." in texto and "Ritmo objetivo: 5:45 min/km." in texto
    assert "fatiga" not in texto


# --- Clima en el mensaje (AC-13, AC-23, AC-24, AC-64) ---


def test_sin_alerta_el_mensaje_no_habla_del_clima():
    texto = build_message(SUAVE, *RANGO, fatigue=3, alert=None)
    assert "lima" not in texto and "lternativ" not in texto


@pytest.mark.parametrize(
    "motivos, pronostico, esperado",
    [
        (("lluvia",), WindowForecast(80, 20.0), "probabilidad de lluvia de 80%"),
        (("calor",), WindowForecast(10, 34.5), "temperatura de hasta 34,5 °C"),
        (
            ("lluvia", "calor"),
            WindowForecast(61, 33.0),
            "probabilidad de lluvia de 61% y temperatura de hasta 33 °C",
        ),
    ],
)
def test_la_alerta_lleva_el_motivo_climatico_especifico(motivos, pronostico, esperado):
    texto = build_message(SUAVE, *RANGO, alert=Alert(motivos, pronostico, JUEVES))
    assert f"Alerta climática: {esperado} en tu horario." in lineas(texto)


def test_la_alerta_informa_el_dia_alternativo_sin_pedir_confirmacion():
    texto = build_message(SUAVE, *RANGO, fatigue=3, alert=LLUVIA)
    assert lineas(texto)[-1] == "Día alternativo sin condición desfavorable: jueves 8/10."
    assert "?" not in texto and "reprogram" not in texto.lower()


def test_la_alerta_sin_dia_alternativo_lo_dice():
    alerta = Alert(("calor",), WindowForecast(0, 36.0), alternative=None)
    texto = build_message(SUAVE, *RANGO, alert=alerta)
    assert lineas(texto)[-1] == SIN_ALTERNATIVA + "."
    assert "Día alternativo" not in texto


def test_sin_clima_el_mensaje_lleva_el_texto_de_clima_no_disponible():
    texto = build_message(CALIDAD, *RANGO, fatigue=4, weather_available=False)
    assert lineas(texto)[-1] == CLIMA_NO_DISPONIBLE
    assert "Alerta" not in texto and "Sesión de hoy: calidad." in texto


# --- Extensión (AC-27) ---


def completo(sesion):
    """El mensaje más largo posible para una sesión: fatiga alta, dos motivos y alternativa."""
    alerta = Alert(("lluvia", "calor"), WindowForecast(100, 39.9), JUEVES)
    return build_message(sesion, *RANGO, fatigue=10, alert=alerta)


@pytest.mark.parametrize("sesion", [SUAVE, NOVATA, CALIDAD])
def test_el_mensaje_completo_no_supera_las_150_palabras(sesion):
    assert word_count(completo(sesion)) <= MAX_WORDS == 150


def test_muchos_bloques_repetidos_se_agrupan_y_entran_en_el_mensaje():
    sesion = Session(LUNES, "suave", (Block("caminata", 0.2), Block("trote", 0.2)) * 40)
    texto = completo(sesion)
    assert "Secuencia: 40 x (0,2 km caminata + 0,2 km trote)." in texto
    assert word_count(texto) <= MAX_WORDS


def test_una_secuencia_demasiado_larga_se_resume_para_no_superar_las_150_palabras():
    bloques = tuple(
        Block("caminata" if i % 2 else "trote", round(0.1 + i * 0.01, 2)) for i in range(60)
    )
    texto = completo(Session(LUNES, "suave", bloques))
    assert word_count(texto) <= MAX_WORDS
    assert (
        "Secuencia: 30 bloques de trote (11,7 km en total), 30 bloques de caminata "
        "(12 km en total), intercalados en el orden de tu plan." in texto
    )
    assert "Alerta climática" in texto and "Día alternativo" in texto


def test_una_sesion_de_calidad_con_muchos_tramos_distintos_tampoco_supera_el_limite():
    bloques = tuple(Block("tramo", round(0.2 + i * 0.01, 2), recovery_seconds=30 + i) for i in range(40))
    sesion = Session(LUNES, "calidad", (Block("trote", 1), *bloques), pace_seconds=300)
    texto = build_message(sesion, *RANGO, fatigue=5, alert=LLUVIA)
    assert word_count(texto) <= MAX_WORDS
    assert "Ritmo objetivo: 5:00 min/km." in texto
    assert "1 bloque de trote (1 km en total), 40 bloques a ritmo objetivo" in texto
