import json
from datetime import date, timedelta

import pytest

from app.guardrails import (
    Adjustment,
    Block,
    InvalidPlan,
    Session,
    WeekSummary,
    apply_guardrails,
    cap_session_km,
    cap_weekly_km,
    check_structure,
    clamp_quality_pace,
    fastest_allowed_pace,
    interval_seconds,
    limit_quality,
    load_caps,
    log_adjustments,
    novice_quality_allowed,
    remove_quality,
    to_easy,
    weekly_km,
)

LUNES = date(2026, 10, 5)
DOMINGO = LUNES + timedelta(days=6)
RITMO = 360  # 6:00 min/km
SEMANA_BUENA = WeekSummary(planned_km=20, completed=1)


def suave(dia, *bloques):
    return Session(LUNES + timedelta(days=dia), "suave", tuple(Block(*b) for b in bloques))


def suaves(*kms):
    return [suave(i, ("trote", km)) for i, km in enumerate(kms)]


def calidad(dia, km=5.0, ritmo=350):
    """Sesión de calidad con un único tramo de `km` y 90 segundos de recuperación."""
    return Session(LUNES + timedelta(days=dia), "calidad", (Block("tramo", km, 90),), ritmo)


def kms(sessions):
    return [s.jog_km for s in sessions]


def tipos(sessions):
    return [s.kind for s in sessions]


# --- Estructura (AC-65) ---


def test_un_plan_con_n_sesiones_en_dias_distintos_es_valido():
    check_structure(suaves(3, 3, 3, 3), 4, LUNES, DOMINGO)


@pytest.mark.parametrize("cantidad", [3, 5])
def test_un_plan_con_otra_cantidad_de_sesiones_se_rechaza(cantidad):
    with pytest.raises(InvalidPlan):
        check_structure(suaves(*[3] * cantidad), 4, LUNES, DOMINGO)


def test_dos_sesiones_el_mismo_dia_se_rechazan():
    with pytest.raises(InvalidPlan):
        check_structure([*suaves(3), suave(0, ("trote", 2))], 2, LUNES, DOMINGO)


@pytest.mark.parametrize(
    "sesion",
    [
        suave(-1, ("trote", 3)),
        suave(7, ("trote", 3)),
        Session(LUNES, "fartlek", (Block("trote", 3),)),
        Session(LUNES, "suave", ()),
        suave(0, ("bici", 3)),
        suave(0, ("trote", -1)),
        suave(0, ("trote", 0)),
        suave(0, ("trote", float("nan"))),
        suave(0, ("tramo", 1, 90)),  # una sesión suave no tiene tramos
        Session(LUNES, "calidad", (Block("tramo", 1, 90),)),  # sin ritmo objetivo
        Session(LUNES, "calidad", (Block("trote", 3),), 350),  # sin tramos
        Session(LUNES, "calidad", (Block("tramo", 1),), 350),  # tramo sin recuperación
    ],
)
def test_una_sesion_invalida_se_rechaza(sesion):
    with pytest.raises(InvalidPlan):
        check_structure([sesion], 1, LUNES, DOMINGO)


def test_un_plan_parcial_solo_admite_dias_desde_la_generacion():
    jueves = LUNES + timedelta(days=3)
    with pytest.raises(InvalidPlan):
        check_structure(suaves(3), 1, jueves, DOMINGO)


def test_una_sesion_de_calidad_con_tramos_y_una_de_caminata_y_trote_son_validas():
    sesiones = [
        suave(0, ("caminata", 0.3), ("trote", 0.2), ("caminata", 0.3), ("trote", 0.2)),
        Session(
            LUNES + timedelta(days=1),
            "calidad",
            (Block("trote", 2), Block("tramo", 1, 90), Block("tramo", 1, 0), Block("trote", 1)),
            350,
        ),
    ]
    check_structure(sesiones, 2, LUNES, DOMINGO)


# --- Volumen (AC-18) ---


def test_el_volumen_semanal_suma_solo_los_km_de_trote():
    assert weekly_km(suaves(1.5, 2.25, 3)) == 6.75


# AC-18: los tramos son trote; la caminata no cuenta.
def test_la_caminata_no_cuenta_en_el_volumen_y_los_tramos_si():
    sesion = Session(
        LUNES, "calidad", (Block("caminata", 1), Block("trote", 2), Block("tramo", 0.5, 60)), 350
    )
    assert sesion.jog_km == 2.5 and weekly_km([sesion]) == 2.5


# AC-15: el tiempo objetivo del tramo sale de su distancia y del ritmo objetivo.
def test_el_tiempo_objetivo_de_un_tramo_es_distancia_por_ritmo():
    assert interval_seconds(0.4, 340) == 136


# --- Novato sin calidad (AC-20) ---


@pytest.mark.parametrize(
    "ritmo, semanas",
    [
        (None, [SEMANA_BUENA] * 3),
        (RITMO, []),
        (RITMO, [SEMANA_BUENA] * 2),
        (RITMO, [SEMANA_BUENA, WeekSummary(20, completed=0), SEMANA_BUENA]),
        (RITMO, [SEMANA_BUENA, SEMANA_BUENA, WeekSummary(20, completed=2, hard_cut=True)]),
    ],
)
def test_el_novato_no_recibe_calidad_si_no_cumple_las_condiciones(ritmo, semanas):
    assert not novice_quality_allowed(ritmo, semanas)


def test_el_novato_con_ritmo_y_3_semanas_buenas_puede_recibir_calidad():
    assert novice_quality_allowed(RITMO, [SEMANA_BUENA] * 3)


# AC-20: la primera semana, aunque sea parcial, cuenta si tiene una sesión Completada.
def test_una_semana_parcial_con_una_completada_cuenta():
    parcial = WeekSummary(4, is_partial=True, completed=1)
    assert novice_quality_allowed(RITMO, [SEMANA_BUENA, SEMANA_BUENA, parcial])


def test_solo_se_miran_las_ultimas_3_semanas():
    mala = WeekSummary(20, completed=0)
    assert novice_quality_allowed(RITMO, [SEMANA_BUENA] * 3 + [mala])


def test_remove_quality_pasa_todas_las_sesiones_a_suave():
    resultado, ajustes = remove_quality([calidad(0), *suaves(0, 2)[1:], calidad(3)])
    assert tipos(resultado) == ["suave", "suave", "suave"]
    assert all(s.pace_seconds is None for s in resultado)
    assert kms(resultado) == [5.0, 2, 5.0]
    assert [a.rule for a in ajustes] == ["novato_sin_calidad"] * 2
    assert ajustes[0] == Adjustment("novato_sin_calidad", "calidad", "suave", LUNES)


# AC-77
def test_una_calidad_pasa_a_suave_como_un_unico_bloque_de_trote():
    sesion = Session(
        LUNES,
        "calidad",
        (Block("trote", 2), Block("tramo", 1, 90), Block("tramo", 1, 90), Block("trote", 1.5)),
        340,
    )
    assert to_easy(sesion) == Session(LUNES, "suave", (Block("trote", 5.5),))


# --- Una sola sesión de calidad (AC-21) ---


def test_con_dos_sesiones_de_calidad_queda_solo_la_primera():
    resultado, ajustes = limit_quality([calidad(4), calidad(1), calidad(6)])
    assert tipos(resultado) == ["suave", "calidad", "suave"]
    assert [a.day for a in ajustes] == [LUNES + timedelta(days=4), LUNES + timedelta(days=6)]


@pytest.mark.parametrize("sesiones", [suaves(3, 3), [calidad(0), *suaves(0, 3)[1:]]])
def test_con_una_o_ninguna_sesion_de_calidad_no_cambia_nada(sesiones):
    assert limit_quality(sesiones) == (sesiones, [])


# --- Ritmo objetivo (AC-22) ---


# AC-22: con 6:00 min/km, entre 6:00 y 5:40 min/km.
def test_el_ritmo_mas_rapido_permitido_para_6_00_es_5_40():
    assert fastest_allowed_pace(360) == 340


@pytest.mark.parametrize(
    "propuesto, esperado", [(300, 340), (339, 340), (340, 340), (350, 350), (360, 360), (400, 360)]
)
def test_el_ritmo_objetivo_se_lleva_al_rango_permitido(propuesto, esperado):
    resultado, ajustes = clamp_quality_pace([calidad(0, ritmo=propuesto)], RITMO)
    assert resultado[0].pace_seconds == esperado
    assert ajustes == (
        [] if propuesto == esperado else [Adjustment("ritmo_objetivo", propuesto, esperado, LUNES)]
    )


def test_el_ritmo_de_las_sesiones_suaves_no_se_toca():
    sesiones = suaves(3, 3)
    assert clamp_quality_pace(sesiones, RITMO) == (sesiones, [])


# --- Topes de carga ---


# AC-61
@pytest.mark.parametrize("nivel, tope", [("novato", 2), ("intermedio", 5), ("avanzado", 10)])
def test_sin_plan_en_la_semana_anterior_aplica_el_tope_inicial(nivel, tope):
    caps = load_caps(nivel, 4, 21.1)
    assert (caps.session_km, caps.session_rule) == (tope, "tope_inicial_sesion")
    assert (caps.weekly_km, caps.weekly_rule) == (tope * 4, "tope_inicial_semanal")


# AC-61: el tope inicial vale aunque haya semanas completas más viejas.
def test_sin_plan_en_la_semana_anterior_no_importan_las_semanas_viejas():
    caps = load_caps("intermedio", 4, 21.1, previous_week=None, last_full_week_km=40)
    assert caps.weekly_rule == "tope_inicial_semanal" and caps.weekly_km == 20


# AC-62
def test_semana_anterior_parcial_sin_semana_completa_previa_aplica_el_tope_inicial():
    parcial = WeekSummary(6, is_partial=True, completed=2)
    caps = load_caps("intermedio", 4, 21.1, previous_week=parcial)
    assert (caps.session_km, caps.weekly_km) == (5, 20)
    assert caps.weekly_rule == "tope_inicial_semanal"


# AC-67: en la primera semana también manda la distancia objetivo si es menor al tope inicial.
def test_la_distancia_objetivo_le_gana_al_tope_inicial_si_es_menor():
    caps = load_caps("avanzado", 3, 5)
    assert (caps.session_km, caps.session_rule) == (5, "distancia_objetivo")
    assert caps.weekly_km == 30


# AC-18
def test_con_semana_anterior_completa_y_registrada_el_tope_es_10_por_ciento():
    caps = load_caps("intermedio", 4, 21.1, previous_week=WeekSummary(20, completed=1))
    assert (caps.weekly_km, caps.weekly_rule) == (22, "tope_10")
    assert (caps.session_km, caps.session_rule) == (21.1, "distancia_objetivo")


# AC-18: sin ningún piso mínimo que lo supere.
def test_el_tope_de_10_por_ciento_no_tiene_piso_minimo():
    caps = load_caps("novato", 3, 10, previous_week=WeekSummary(0.5, completed=1))
    assert caps.weekly_km == 0.55


# AC-63
def test_semana_anterior_sin_sesiones_completadas_no_admite_aumento():
    caps = load_caps("intermedio", 4, 21.1, previous_week=WeekSummary(20, completed=0))
    assert (caps.weekly_km, caps.weekly_rule) == (20, "sin_aumento")


# AC-74
def test_semana_anterior_parcial_usa_la_ultima_semana_completa_como_base():
    parcial = WeekSummary(6, is_partial=True, completed=1)
    caps = load_caps("intermedio", 4, 21.1, previous_week=parcial, last_full_week_km=30)
    assert (caps.weekly_km, caps.weekly_rule) == (33, "tope_10")


# AC-74 + AC-63: parcial sin Completadas, la base sigue siendo la última semana completa.
def test_semana_anterior_parcial_sin_completadas_no_supera_la_ultima_semana_completa():
    parcial = WeekSummary(6, is_partial=True, completed=0)
    caps = load_caps("intermedio", 4, 21.1, previous_week=parcial, last_full_week_km=30)
    assert (caps.weekly_km, caps.weekly_rule) == (30, "sin_aumento")


# AC-61, AC-67
def test_cada_sesion_se_recorta_al_tope_por_sesion():
    resultado, ajustes = cap_session_km(suaves(4, 5, 5.01, 12), 5, "tope_inicial_sesion")
    assert kms(resultado) == [4, 5, 5, 5]
    assert ajustes == [
        Adjustment("tope_inicial_sesion", 5.01, 5, LUNES + timedelta(days=2)),
        Adjustment("tope_inicial_sesion", 12, 5, LUNES + timedelta(days=3)),
    ]


# AC-18
def test_el_volumen_semanal_se_recorta_en_proporcion_hasta_el_tope():
    resultado, ajustes = cap_weekly_km(suaves(10, 10, 5), 22, "tope_10")
    assert kms(resultado) == [8.8, 8.8, 4.4]
    assert ajustes == [Adjustment("tope_10", 25, 22)]


# AC-76: se recorta cada bloque de trote; caminata, orden y cantidad de bloques no cambian.
def test_el_recorte_por_sesion_multiplica_cada_bloque_de_trote_por_el_mismo_factor():
    larga = suave(0, ("caminata", 0.5), ("trote", 6), ("caminata", 0.5), ("trote", 2))
    corta = suave(1, ("trote", 3))
    resultado, ajustes = cap_session_km([larga, corta], 5, "distancia_objetivo")
    assert resultado == [
        suave(0, ("caminata", 0.5), ("trote", 3.75), ("caminata", 0.5), ("trote", 1.25)),
        corta,
    ]
    assert ajustes == [Adjustment("distancia_objetivo", 8, 5, LUNES)]


# AC-76: los tramos se recortan, y la recuperación y el ritmo objetivo quedan iguales.
def test_el_recorte_de_una_calidad_conserva_recuperaciones_y_ritmo():
    sesion = Session(
        LUNES,
        "calidad",
        (Block("trote", 2), Block("tramo", 1, 90), Block("tramo", 1, 60), Block("trote", 2)),
        340,
    )
    (recortada,), _ = cap_session_km([sesion], 5, "distancia_objetivo")
    assert recortada.blocks == (
        Block("trote", 1.66),
        Block("tramo", 0.83, 90),
        Block("tramo", 0.83, 60),
        Block("trote", 1.66),
    )
    assert recortada.pace_seconds == 340 and recortada.jog_km <= 5
    assert interval_seconds(recortada.blocks[1].km, recortada.pace_seconds) == 282


# AC-76: primero el tope por sesión y después el semanal, sobre el volumen ya recortado.
def test_el_tope_por_sesion_se_aplica_antes_que_el_semanal():
    propuesto = [suave(0, ("caminata", 1), ("trote", 4), ("trote", 4)), *suaves(0, 5, 5, 5)[1:]]
    resultado, ajustes = apply_guardrails(
        propuesto,
        level="intermedio",
        frequency=4,
        target_km=5,
        pace_seconds=RITMO,
        previous_week=WeekSummary(15, completed=0),
    )
    assert ajustes == [
        Adjustment("distancia_objetivo", 8, 5, LUNES),
        Adjustment("sin_aumento", 20, 14.99),
    ]
    assert resultado[0].blocks == (Block("caminata", 1), Block("trote", 1.87), Block("trote", 1.87))
    assert kms(resultado) == [3.74, 3.75, 3.75, 3.75]


def test_un_recorte_que_deja_un_bloque_en_cero_se_rechaza():
    with pytest.raises(InvalidPlan):
        cap_weekly_km([suave(0, ("trote", 0.01), ("trote", 10))], 5, "tope_10")


def test_el_recorte_semanal_nunca_supera_el_tope_por_redondeo():
    resultado, _ = cap_weekly_km(suaves(3.33, 3.33, 3.34, 7.77), 10, "tope_10")
    assert weekly_km(resultado) <= 10
    assert weekly_km(resultado) > 9.9


@pytest.mark.parametrize("total", [(10, 10), (10, 12)])
def test_un_volumen_dentro_del_tope_no_se_toca(total):
    sesiones = suaves(*total)
    assert cap_weekly_km(sesiones, 22, "tope_10") == (sesiones, [])


# --- Todos los guardarraíles juntos ---

INTERMEDIO = dict(level="intermedio", frequency=4, target_km=21.1, pace_seconds=RITMO)
NOVATO = dict(level="novato", frequency=3, target_km=10, pace_seconds=None)


def test_un_plan_que_ya_cumple_no_se_modifica():
    sesiones = [*suaves(5, 4, 5), calidad(3, km=5, ritmo=345)]
    assert apply_guardrails(sesiones, **INTERMEDIO) == (sesiones, [])


# AC-20, AC-61: el riesgo principal del PRD, un plan excesivo para una novata.
def test_plan_excesivo_para_una_novata_en_su_primera_semana():
    propuesto = [*suaves(6, 8), calidad(2, km=12, ritmo=300)]
    resultado, ajustes = apply_guardrails(propuesto, **NOVATO)
    assert tipos(resultado) == ["suave"] * 3
    assert kms(resultado) == [2, 2, 2]
    assert weekly_km(resultado) <= 2 * 3
    assert {a.rule for a in ajustes} == {"novato_sin_calidad", "tope_inicial_sesion"}


# AC-20: con ritmo y 3 semanas buenas, el novato conserva una sola sesión de calidad.
def test_novato_habilitado_conserva_una_sesion_de_calidad_con_el_ritmo_limitado():
    propuesto = [calidad(0, km=3, ritmo=300), *suaves(0, 3)[1:], calidad(2, km=3)]
    resultado, ajustes = apply_guardrails(
        propuesto,
        **{**NOVATO, "pace_seconds": RITMO},
        previous_week=SEMANA_BUENA,
        recent_weeks=[SEMANA_BUENA] * 3,
    )
    assert tipos(resultado) == ["calidad", "suave", "suave"]
    assert resultado[0].pace_seconds == 340
    assert [a.rule for a in ajustes] == ["una_sesion_de_calidad", "ritmo_objetivo"]


# AC-18, AC-21, AC-22, AC-67
def test_plan_de_intermedio_que_rompe_varios_guardarrailes():
    propuesto = [calidad(0, km=8, ritmo=320), calidad(1, km=8), *suaves(0, 0, 30, 10)[2:]]
    resultado, ajustes = apply_guardrails(
        propuesto, **INTERMEDIO, previous_week=WeekSummary(30, completed=2)
    )
    assert tipos(resultado) == ["calidad", "suave", "suave", "suave"]
    assert resultado[0].pace_seconds == 340
    assert max(kms(resultado)) <= 21.1
    assert weekly_km(resultado) <= 33
    assert [a.rule for a in ajustes] == [
        "una_sesion_de_calidad",
        "ritmo_objetivo",
        "distancia_objetivo",
        "tope_10",
    ]
    assert ajustes[-1].before == 47.1 and ajustes[-1].after == weekly_km(resultado)


# AC-63
def test_semana_anterior_sin_completadas_recorta_al_volumen_anterior():
    resultado, ajustes = apply_guardrails(
        suaves(6, 6, 6, 6), **INTERMEDIO, previous_week=WeekSummary(20, completed=0)
    )
    assert weekly_km(resultado) == 20
    assert ajustes == [Adjustment("sin_aumento", 24, 20)]


def test_los_km_se_normalizan_a_centesimas_antes_de_comparar():
    resultado, ajustes = apply_guardrails(suaves(3.3333, 4.9999), **INTERMEDIO)
    assert kms(resultado) == [3.33, 5.0] and ajustes == []


def test_una_calidad_sin_ritmo_actual_no_se_puede_limitar():
    with pytest.raises(InvalidPlan):
        apply_guardrails([calidad(0)], **{**INTERMEDIO, "pace_seconds": None})


# RNF-02
def test_cada_guardarrail_aplicado_se_registra_con_antes_y_despues(capsys):
    log_adjustments(
        [Adjustment("tope_10", 25, 22), Adjustment("ritmo_objetivo", 320, 340, LUNES)], user_id=7
    )
    registros = [json.loads(linea) for linea in capsys.readouterr().out.strip().splitlines()]
    assert registros[0]["event"] == "guardrail"
    assert (registros[0]["regla"], registros[0]["antes"], registros[0]["despues"]) == ("tope_10", 25, 22)
    assert registros[0]["fecha"] is None and registros[0]["user_id"] == 7
    assert registros[1]["fecha"] == "2026-10-05"
