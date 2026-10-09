import json
import sqlite3
from datetime import date, timedelta

import pytest
from google.genai import errors

from app import plan, planner
from app.auth import RENEWED_TOKEN_HEADER
from app.plan import partial_session_count

LUNES = date(2026, 10, 5)
JUEVES = LUNES + timedelta(days=3)
PERFIL = {
    "level": "intermedio",
    "frequency": 3,
    "target_km": 21.1,
    "pace": "6:00",
    "city": "Rosario",
    "window_start": "07:00",
    "window_end": "09:00",
    "send_time": "06:30",
}
NOVATA = {**PERFIL, "level": "novato", "target_km": 10, "pace": None}
NO_DISPONIBLE = "No es posible generar un plan en este momento."


def trote(dia, km, desde=LUNES):
    return {"date": str(desde + timedelta(days=dia)), "kind": "suave", "blocks": [{"kind": "trote", "km": km}]}


def caminata_y_trote(dia, km, desde=LUNES):
    bloques = [{"kind": "caminata", "km": 0.4}, {"kind": "trote", "km": km / 2}] * 2
    return {"date": str(desde + timedelta(days=dia)), "kind": "suave", "blocks": bloques}


def calidad(dia, ritmo="5:45", desde=LUNES):
    return {
        "date": str(desde + timedelta(days=dia)),
        "kind": "calidad",
        "target_pace": ritmo,
        "blocks": [
            {"kind": "trote", "km": 2},
            {"kind": "tramo", "km": 0.4, "recovery_seconds": 90},
            {"kind": "tramo", "km": 0.4, "recovery_seconds": 60},
            {"kind": "trote", "km": 1},
        ],
    }


@pytest.fixture
def gemini(monkeypatch):
    """Reemplaza la llamada a Gemini y guarda los prompts recibidos; nunca se sale a la red."""
    estado = {"respuesta": None, "prompts": []}

    def falso(prompt):
        estado["prompts"].append(prompt)
        if isinstance(estado["respuesta"], Exception):
            raise estado["respuesta"]
        return json.dumps({"sessions": estado["respuesta"]})

    monkeypatch.setattr(planner, "_call_gemini", falso)
    return estado


@pytest.fixture
def hoy(monkeypatch):
    def _hoy(fecha):
        monkeypatch.setattr(plan, "today", lambda: fecha)

    _hoy(LUNES)
    return _hoy


@pytest.fixture
def usuario(client, auth, conn):
    """Registra un usuario con perfil y chat vinculado, y devuelve sus headers."""

    def _usuario(perfil=PERFIL, chat=1001, email="pepe@example.com"):
        headers = auth(email)
        assert client.put("/profile", json=perfil, headers=headers).status_code == 200
        conn.execute("UPDATE users SET telegram_chat_id = ? WHERE email = ?", (chat, email))
        conn.commit()
        return headers

    return _usuario


def planes(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM plans ORDER BY week_start")]


def eventos(capsys, nombre):
    lineas = capsys.readouterr().out.strip().splitlines()
    return [r for r in map(json.loads, lineas) if r["event"] == nombre]


# AC-56
def test_post_plan_genera_persiste_y_devuelve_201(client, usuario, gemini, hoy, conn):
    gemini["respuesta"] = [trote(0, 4), calidad(2), trote(5, 5)]
    r = client.post("/plan", headers=usuario())

    assert r.status_code == 201 and RENEWED_TOKEN_HEADER in r.headers
    cuerpo = r.json()
    assert cuerpo["week_start"] == "2026-10-05" and cuerpo["is_partial"] is False
    assert cuerpo["planned_km"] == 12.8
    assert [s["date"] for s in cuerpo["sessions"]] == ["2026-10-05", "2026-10-07", "2026-10-10"]
    assert [s["kind"] for s in cuerpo["sessions"]] == ["suave", "calidad", "suave"]

    (guardado,) = planes(conn)
    assert (guardado["week_start"], guardado["planned_km"], guardado["is_partial"]) == ("2026-10-05", 12.8, 0)
    sesiones = conn.execute("SELECT * FROM sessions ORDER BY date").fetchall()
    assert [(s["date"], s["kind"], s["jog_km"]) for s in sesiones] == [
        ("2026-10-05", "suave", 4.0),
        ("2026-10-07", "calidad", 3.8),
        ("2026-10-10", "suave", 5.0),
    ]
    assert json.loads(sesiones[0]["detail"]) == {"blocks": [{"kind": "trote", "km": 4.0}]}
    assert json.loads(sesiones[1]["detail"])["target_pace"] == "5:45"


# AC-65: días fuera de orden se devuelven ordenados; se pide la frecuencia cargada.
def test_el_prompt_pide_tantas_sesiones_como_la_frecuencia(client, usuario, gemini, hoy):
    gemini["respuesta"] = [trote(5, 5), trote(0, 4), trote(2, 3)]
    r = client.post("/plan", headers=usuario())
    assert [s["date"] for s in r.json()["sessions"]] == ["2026-10-05", "2026-10-07", "2026-10-10"]
    assert "Cantidad de sesiones: 3" in gemini["prompts"][0]
    assert "Sin semanas anteriores" in gemini["prompts"][0]


# AC-15
def test_la_sesion_de_calidad_explicita_tiempo_objetivo_y_recuperacion_por_tramo(client, usuario, gemini, hoy):
    gemini["respuesta"] = [trote(0, 4), calidad(2, ritmo="5:45"), trote(5, 5)]
    sesion = client.post("/plan", headers=usuario()).json()["sessions"][1]
    assert sesion["target_pace"] == "5:45"
    tramos = [b for b in sesion["blocks"] if b["kind"] == "tramo"]
    assert tramos == [
        {"kind": "tramo", "km": 0.4, "target_seconds": 138, "recovery_seconds": 90},
        {"kind": "tramo", "km": 0.4, "target_seconds": 138, "recovery_seconds": 60},
    ]


# AC-22: si el ritmo se limita, el tiempo objetivo del tramo se calcula con el ritmo nuevo.
def test_un_ritmo_objetivo_demasiado_rapido_se_limita(client, usuario, gemini, hoy, capsys):
    gemini["respuesta"] = [trote(0, 4), calidad(2, ritmo="5:00"), trote(5, 5)]
    sesion = client.post("/plan", headers=usuario()).json()["sessions"][1]
    assert sesion["target_pace"] == "5:40"
    assert sesion["blocks"][1]["target_seconds"] == 136
    (ajuste,) = eventos(capsys, "guardrail")
    assert (ajuste["regla"], ajuste["antes"], ajuste["despues"]) == ("ritmo_objetivo", 300, 340)


# AC-14, AC-10
def test_la_primera_semana_de_una_novata_tiene_bloques_de_caminata_y_trote(client, usuario, gemini, hoy):
    gemini["respuesta"] = [caminata_y_trote(d, 1.6) for d in (0, 2, 4)]
    r = client.post("/plan", headers=usuario(NOVATA))
    assert r.status_code == 201
    for sesion in r.json()["sessions"]:
        assert [b["kind"] for b in sesion["blocks"]] == ["caminata", "trote", "caminata", "trote"]
        assert all(b["km"] > 0 for b in sesion["blocks"])


# AC-14
def test_un_plan_de_novata_sin_bloques_alternados_no_se_entrega(client, usuario, gemini, hoy, conn, capsys):
    gemini["respuesta"] = [trote(d, 1.5) for d in (0, 2, 4)]
    r = client.post("/plan", headers=usuario(NOVATA))
    assert r.status_code == 503 and r.json()["detail"] == NO_DISPONIBLE
    assert planes(conn) == []
    assert len(eventos(capsys, "plan_invalido")) == 1


# AC-20, AC-61: el riesgo principal del PRD, de punta a punta.
def test_un_plan_excesivo_para_una_novata_se_recorta_antes_de_entregarlo(client, usuario, gemini, hoy, conn, capsys):
    gemini["respuesta"] = [caminata_y_trote(d, 8) for d in (0, 2, 4)]
    r = client.post("/plan", headers=usuario(NOVATA))

    assert r.status_code == 201
    assert [s["jog_km"] for s in r.json()["sessions"]] == [2, 2, 2]
    assert r.json()["sessions"][0]["blocks"] == [
        {"kind": "caminata", "km": 0.4, "target_seconds": None, "recovery_seconds": None},
        {"kind": "trote", "km": 1.0, "target_seconds": None, "recovery_seconds": None},
    ] * 2
    assert planes(conn)[0]["planned_km"] == 6
    ajustes = eventos(capsys, "guardrail")
    assert [(a["regla"], a["antes"], a["despues"]) for a in ajustes] == [("tope_inicial_sesion", 8, 2)] * 3


# AC-08, AC-09, AC-11
@pytest.mark.parametrize(
    "perfil, chat, mensaje",
    [
        ({**PERFIL, "pace": None}, 1001, "No hay datos sobre tu ritmo cargados en tu perfil"),
        ({**PERFIL, "city": None}, 1001, "No hay datos sobre tu ciudad cargados en tu perfil"),
        ({**PERFIL, "frequency": None}, 1001, "No hay datos sobre tu frecuencia cargados en tu perfil"),
        (PERFIL, None, "Tu cuenta no tiene un chat de Telegram vinculado"),
    ],
)
def test_un_perfil_incompleto_se_rechaza_con_422_sin_crear_plan(
    client, usuario, gemini, hoy, conn, perfil, chat, mensaje
):
    r = client.post("/plan", headers=usuario(perfil, chat=chat))
    assert r.status_code == 422 and r.json()["detail"] == mensaje
    assert planes(conn) == [] and gemini["prompts"] == []


# AC-58
def test_con_un_plan_para_la_semana_en_curso_devuelve_409(client, usuario, gemini, hoy, conn):
    gemini["respuesta"] = [trote(0, 4), trote(2, 4), trote(4, 4)]
    headers = usuario()
    assert client.post("/plan", headers=headers).status_code == 201
    hoy(JUEVES)
    assert client.post("/plan", headers=headers).status_code == 409
    assert len(planes(conn)) == 1 and len(gemini["prompts"]) == 1


# AC-58: otro request crea el plan de la semana mientras se espera al modelo.
def test_si_el_plan_aparece_durante_la_generacion_devuelve_409(client, usuario, hoy, conn, monkeypatch):
    def gemini_lento(prompt):
        conn.execute(
            "INSERT INTO plans (user_id, week_start, planned_km, created_at) VALUES (1, ?, 9, 'x')",
            (str(LUNES),),
        )
        conn.commit()
        return json.dumps({"sessions": [trote(0, 4), trote(2, 4), trote(4, 4)]})

    monkeypatch.setattr(planner, "_call_gemini", gemini_lento)
    r = client.post("/plan", headers=usuario())
    assert r.status_code == 409
    assert [p["planned_km"] for p in planes(conn)] == [9]
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


# Un choque al guardar que no es por el plan de la semana no se informa como 409.
def test_un_error_de_guardado_que_no_es_por_plan_existente_no_devuelve_409(client, usuario, gemini, hoy, conn):
    headers = usuario()
    # Sesión huérfana, como la que deja un borrado manual de plans sin claves foráneas.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO sessions (plan_id, user_id, date, kind, jog_km, detail) VALUES (1, 1, ?, 'suave', 2, '{}')",
        (str(LUNES),),
    )
    conn.commit()
    gemini["respuesta"] = [trote(0, 4), trote(2, 4), trote(4, 4)]

    with pytest.raises(sqlite3.IntegrityError):
        client.post("/plan", headers=headers)
    assert planes(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_el_plan_de_otro_usuario_no_bloquea_el_propio(client, usuario, gemini, hoy, conn):
    gemini["respuesta"] = [trote(0, 4), trote(2, 4), trote(4, 4)]
    assert client.post("/plan", headers=usuario()).status_code == 201
    mich = usuario(chat=2002, email="mich@example.com")
    assert client.post("/plan", headers=mich).status_code == 201
    assert len(planes(conn)) == 2
    assert conn.execute("SELECT COUNT(DISTINCT user_id) FROM sessions").fetchone()[0] == 2


# AC-17
@pytest.mark.parametrize(
    "error",
    [
        errors.ServerError(503, {"error": {"message": "caído"}}),
        errors.ClientError(429, {"error": {"message": "rate limit"}}),
    ],
)
def test_si_gemini_falla_devuelve_503_sin_crear_plan(client, usuario, gemini, hoy, conn, error):
    gemini["respuesta"] = error
    r = client.post("/plan", headers=usuario())
    assert r.status_code == 503 and r.json()["detail"] == NO_DISPONIBLE
    assert planes(conn) == []


# AC-17
def test_si_gemini_no_responde_a_tiempo_devuelve_503(client, usuario, hoy, conn, monkeypatch):
    import time

    monkeypatch.setattr(planner, "GEMINI_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(planner, "_call_gemini", lambda prompt: time.sleep(0.4))
    r = client.post("/plan", headers=usuario())
    assert r.status_code == 503 and r.json()["detail"] == NO_DISPONIBLE
    assert planes(conn) == []


# AC-65
@pytest.mark.parametrize(
    "sesiones",
    [
        [trote(0, 4), trote(2, 4)],
        [trote(0, 4), trote(2, 4), trote(4, 4), trote(5, 4)],
        [trote(0, 4), trote(0, 4), trote(4, 4)],
        [trote(0, 4), trote(2, 4), trote(7, 4)],
    ],
)
def test_un_plan_con_otra_cantidad_o_dias_invalidos_no_se_entrega(client, usuario, gemini, hoy, conn, sesiones):
    gemini["respuesta"] = sesiones
    assert client.post("/plan", headers=usuario()).status_code == 503
    assert planes(conn) == []


# AC-57
def test_un_plan_pedido_a_mitad_de_semana_cubre_desde_hoy_hasta_el_domingo(client, usuario, gemini, hoy, conn):
    hoy(JUEVES)
    gemini["respuesta"] = [trote(3, 4), trote(5, 4)]
    r = client.post("/plan", headers=usuario())

    assert r.status_code == 201
    assert r.json()["week_start"] == "2026-10-05" and r.json()["is_partial"] is True
    assert [s["date"] for s in r.json()["sessions"]] == ["2026-10-08", "2026-10-10"]
    assert planes(conn)[0]["is_partial"] == 1
    prompt = gemini["prompts"][0]
    assert "Días disponibles: 2026-10-08 (jueves)" in prompt and "2026-10-11 (domingo)" in prompt
    assert "2026-10-07" not in prompt and "Cantidad de sesiones: 2" in prompt


# AC-57
def test_un_plan_parcial_no_admite_sesiones_en_dias_ya_pasados(client, usuario, gemini, hoy, conn):
    hoy(JUEVES)
    gemini["respuesta"] = [trote(1, 4), trote(5, 4)]
    assert client.post("/plan", headers=usuario()).status_code == 503
    assert planes(conn) == []


@pytest.mark.parametrize(
    "frecuencia, dias, esperado",
    [(3, 4, 2), (7, 4, 4), (1, 6, 1), (1, 1, 1), (7, 1, 1), (5, 6, 5), (4, 2, 2)],
)
def test_las_sesiones_de_un_plan_parcial_son_proporcionales_a_los_dias_restantes(frecuencia, dias, esperado):
    assert partial_session_count(frecuencia, dias) == esperado


# AC-63: sin registro de sesiones, la semana anterior nunca tiene Completadas.
def test_la_segunda_semana_no_supera_el_volumen_de_la_anterior(client, usuario, gemini, hoy, conn, capsys):
    headers = usuario()
    gemini["respuesta"] = [trote(0, 4), trote(2, 4), trote(4, 4)]
    client.post("/plan", headers=headers)
    capsys.readouterr()

    siguiente = LUNES + timedelta(weeks=1)
    hoy(siguiente)
    gemini["respuesta"] = [trote(d, 8, desde=siguiente) for d in (0, 2, 4)]
    r = client.post("/plan", headers=headers)

    assert r.status_code == 201 and r.json()["planned_km"] == 12
    assert [p["planned_km"] for p in planes(conn)] == [12, 12]
    (ajuste,) = eventos(capsys, "guardrail")
    assert (ajuste["regla"], ajuste["antes"], ajuste["despues"]) == ("sin_aumento", 24, 12)
    # El prompt lleva el resumen semanal, no las sesiones.
    prompt = gemini["prompts"][1]
    assert "- Semana del 2026-10-05: 12 km de trote planificados, semana completa" in prompt
    assert "Máximo 12 km de trote en toda la semana" in prompt
    assert "2026-10-07" not in prompt


# AC-62
def test_tras_una_semana_parcial_sin_semana_completa_previa_rige_el_tope_inicial(client, usuario, gemini, hoy):
    headers = usuario()
    hoy(JUEVES)
    gemini["respuesta"] = [trote(3, 2), trote(5, 2)]
    client.post("/plan", headers=headers)

    siguiente = LUNES + timedelta(weeks=1)
    hoy(siguiente)
    gemini["respuesta"] = [trote(d, 9, desde=siguiente) for d in (0, 2, 4)]
    r = client.post("/plan", headers=headers)
    assert [s["jog_km"] for s in r.json()["sessions"]] == [5, 5, 5]


# AC-74
def test_tras_una_semana_parcial_la_base_es_la_ultima_semana_completa(client, usuario, gemini, hoy, conn):
    headers = usuario()
    gemini["respuesta"] = [trote(0, 5), trote(2, 5), trote(4, 5)]
    client.post("/plan", headers=headers)  # semana completa: 15 km

    segunda = LUNES + timedelta(weeks=1)
    hoy(segunda + timedelta(days=3))
    gemini["respuesta"] = [trote(3, 2, desde=segunda), trote(5, 2, desde=segunda)]
    client.post("/plan", headers=headers)  # semana parcial: 4 km

    tercera = LUNES + timedelta(weeks=2)
    hoy(tercera)
    gemini["respuesta"] = [trote(d, 9, desde=tercera) for d in (0, 2, 4)]
    r = client.post("/plan", headers=headers)
    assert r.json()["planned_km"] == 15
    assert [(p["planned_km"], p["is_partial"]) for p in planes(conn)] == [(15, 0), (4, 1), (15, 0)]


# AC-61: sin plan en la semana anterior vuelve a regir el tope inicial.
def test_tras_una_semana_sin_plan_vuelve_el_tope_inicial(client, usuario, gemini, hoy):
    headers = usuario()
    gemini["respuesta"] = [trote(0, 5), trote(2, 5), trote(4, 5)]
    client.post("/plan", headers=headers)

    tercera = LUNES + timedelta(weeks=2)
    hoy(tercera)
    gemini["respuesta"] = [trote(d, 9, desde=tercera) for d in (0, 2, 4)]
    r = client.post("/plan", headers=headers)
    assert [s["jog_km"] for s in r.json()["sessions"]] == [5, 5, 5]


# AC-21
def test_solo_queda_una_sesion_de_calidad_por_semana(client, usuario, gemini, hoy):
    gemini["respuesta"] = [calidad(0), calidad(2), trote(4, 4)]
    sesiones = client.post("/plan", headers=usuario()).json()["sessions"]
    assert [s["kind"] for s in sesiones] == ["calidad", "suave", "suave"]
    assert sesiones[1]["target_pace"] is None
    assert sesiones[1]["blocks"] == [
        {"kind": "trote", "km": 3.8, "target_seconds": None, "recovery_seconds": None}
    ]


# AC-73
def test_post_plan_exige_token(client, gemini):
    assert client.post("/plan").status_code == 401
    assert gemini["prompts"] == []


def test_la_fecha_de_hoy_es_la_de_buenos_aires():
    assert isinstance(plan.today(), date)
