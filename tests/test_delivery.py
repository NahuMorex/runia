import asyncio
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from google.genai import errors
from telegram.error import TimedOut

from app import bot, weather, writer
from app.daily import word_count
from tests.conftest import PERFIL_CORREDOR as PERFIL

BA = ZoneInfo("America/Argentina/Buenos_Aires")
LUNES = date(2026, 10, 5)
JUEVES = LUNES + timedelta(days=3)
CHAT = 1001
CLIMA_NO_DISPONIBLE = "Clima no disponible: seguí las pautas base de ritmo e hidratación."
SIN_ALTERNATIVA = "No hay una alternativa disponible esta semana"
CALIDAD = (
    LUNES,
    "calidad",
    {
        "target_pace": "5:45",
        "blocks": [
            {"kind": "trote", "km": 2},
            {"kind": "tramo", "km": 0.4, "target_seconds": 138, "recovery_seconds": 90},
            {"kind": "tramo", "km": 0.4, "target_seconds": 138, "recovery_seconds": 90},
        ],
    },
)
NOVATA = (
    LUNES,
    "suave",
    {"blocks": [{"kind": "caminata", "km": 0.4}, {"kind": "trote", "km": 0.4}] * 3},
)


def a_las(hora, minuto=0, dia=LUNES):
    return datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=BA)


def registros(capsys, evento):
    lineas = capsys.readouterr().out.strip().splitlines()
    return [r for r in map(json.loads, lineas) if r["event"] == evento]


def pronostico(*dias):
    """Respuesta de OpenWeatherMap con (lluvia de 0 a 1, temperatura) por día, desde el lunes."""
    items = [
        {"dt": int(a_las(hora, dia=LUNES + timedelta(days=n)).timestamp()), "main": {"temp": temp}, "pop": pop}
        for n, (pop, temp) in enumerate(dias)
        for hora in (6, 9, 12)
    ]
    return httpx.Response(200, json={"list": items})


BUENO, LLUVIA, CALOR = (0.1, 20.0), (0.8, 20.0), (0.1, 35.0)


@pytest.fixture
def clima(monkeypatch):
    """Reemplaza la consulta a OpenWeatherMap y guarda las ciudades pedidas; no se sale a la red."""
    estado = {"respuesta": pronostico(BUENO, BUENO, BUENO, BUENO), "ciudades": []}

    def falso(city):
        estado["ciudades"].append(city)
        if isinstance(estado["respuesta"], Exception):
            raise estado["respuesta"]
        return estado["respuesta"]

    monkeypatch.setattr(weather, "_fetch", falso)
    return estado


@pytest.fixture
def gemini(monkeypatch):
    """Reemplaza la llamada a Gemini por una redacción que conserva los datos; no se sale a la red."""
    estado = {"respuesta": None, "prompts": []}

    def falso(prompt):
        estado["prompts"].append(prompt)
        if isinstance(estado["respuesta"], Exception):
            raise estado["respuesta"]
        if estado["respuesta"] is not None:
            return estado["respuesta"]
        datos = prompt.split("\n", 1)[1].split("\n\nCopiá textual:")[0]
        return "Hola, va tu sesión. " + datos.replace("\n", " ")

    monkeypatch.setattr(writer, "_call_gemini", falso)
    return estado


def enviados(telegram):
    return [(c.kwargs["chat_id"], c.kwargs["text"]) for c in telegram.send_message.call_args_list]


def flujo(telegram, respuesta="5", chat=CHAT):
    """Hora de envío, pregunta de fatiga y respuesta; devuelve el mensaje diario enviado."""
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    assert asyncio.run(bot.handle_text(telegram, chat, respuesta, a_las(6, 40))) is None
    (mensaje,) = [texto for c, texto in enviados(telegram) if c == chat and texto != bot.FATIGUE_QUESTION_TEXT]
    return mensaje


# --- De punta a punta (RF-08, RF-15) ---


def test_pregunta_respuesta_clima_redaccion_y_envio(corredor, telegram, clima, gemini, capsys):
    corredor(sesiones=(CALIDAD,))

    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    assert enviados(telegram) == [(CHAT, bot.FATIGUE_QUESTION_TEXT)]
    # AC-23: el clima ya se evaluó; el mensaje no se redacta hasta la respuesta (AC-47).
    assert clima["ciudades"] == ["Rosario"] and gemini["prompts"] == []

    asyncio.run(bot.handle_text(telegram, CHAT, "5", a_las(6, 40)))

    (_, (chat, mensaje)) = enviados(telegram)
    assert chat == CHAT and mensaje.startswith("Hola, va tu sesión. Sesión de hoy: calidad.")
    assert "Horario sugerido: 07:00 a 09:00." in mensaje and "Ritmo objetivo: 5:45 min/km." in mensaje
    assert "2 km trote, 2 x 0,4 km a ritmo objetivo (en 2:18, recuperación 90 s)" in mensaje
    assert "Alerta" not in mensaje and word_count(mensaje) <= 150
    assert clima["ciudades"] == ["Rosario"] and len(gemini["prompts"]) == 1
    (registro,) = registros(capsys, "mensaje_diario")
    assert (registro["fatiga"], registro["fecha"]) == (5, "2026-10-05")
    assert registro["palabras"] == word_count(mensaje) and "duracion_ms" in registro


def test_si_gemini_falla_se_envia_el_mensaje_de_la_plantilla(corredor, telegram, clima, gemini):
    corredor(sesiones=(LUNES,))
    gemini["respuesta"] = errors.ServerError(503, {"error": {"message": "caído"}})

    assert flujo(telegram).splitlines() == [
        "Sesión de hoy: suave.",
        "Horario sugerido: 07:00 a 09:00.",
        "Secuencia: 5 km trote.",
    ]


def test_una_redaccion_que_inventa_datos_se_reemplaza_por_la_plantilla(corredor, telegram, clima, gemini):
    corredor(sesiones=(LUNES,))
    gemini["respuesta"] = "Hoy corré 12 km a fondo."
    assert flujo(telegram).splitlines()[-1] == "Secuencia: 5 km trote."


# AC-14, AC-28
def test_el_mensaje_de_una_novata_lleva_la_secuencia_de_caminata_y_trote(corredor, telegram, clima, gemini):
    corredor(sesiones=(NOVATA,), perfil={**PERFIL, "level": "novato", "pace": None})
    assert "3 x (0,4 km caminata + 0,4 km trote)" in flujo(telegram)


# --- Fatiga (RF-16, AC-47, AC-48) ---


def test_con_fatiga_de_8_la_sesion_de_calidad_se_envia_como_suave_recuperacion(
    corredor, telegram, clima, gemini, capsys
):
    corredor(sesiones=(CALIDAD,))

    mensaje = flujo(telegram, respuesta="8")

    assert "Sesión de hoy: suave/recuperación." in mensaje and "Secuencia: 2,8 km trote." in mensaje
    assert "Ritmo objetivo" not in mensaje
    (registro,) = registros(capsys, "guardrail")
    assert (registro["regla"], registro["antes"], registro["despues"]) == ("fatiga_alta", "calidad", "suave")


def test_el_ajuste_por_fatiga_no_modifica_el_plan_guardado(corredor, telegram, clima, gemini, conn):
    corredor(sesiones=(CALIDAD,))
    flujo(telegram, respuesta="10")
    fila = conn.execute("SELECT kind, detail FROM sessions").fetchone()
    assert fila["kind"] == "calidad" and json.loads(fila["detail"]) == CALIDAD[2]


def test_con_fatiga_de_7_la_sesion_de_calidad_no_cambia(corredor, telegram, clima, gemini, capsys):
    corredor(sesiones=(CALIDAD,))
    assert "Sesión de hoy: calidad." in flujo(telegram, respuesta="7")
    assert registros(capsys, "guardrail") == []


# AC-47: sin respuesta en 2 horas, el mensaje sale con la sesión planificada.
def test_sin_respuesta_en_2_horas_se_envia_la_sesion_planificada(corredor, telegram, clima, gemini, capsys):
    corredor(sesiones=(CALIDAD,))
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    asyncio.run(bot.tick(telegram, a_las(8, 29)))
    assert len(enviados(telegram)) == 1

    asyncio.run(bot.tick(telegram, a_las(8, 30)))
    asyncio.run(bot.tick(telegram, a_las(8, 31)))

    (_, (chat, mensaje)) = enviados(telegram)
    assert chat == CHAT and "Sesión de hoy: calidad." in mensaje and "fatiga" not in mensaje
    assert registros(capsys, "mensaje_diario")[0]["fatiga"] is None


# AC-75
def test_una_respuesta_invalida_no_dispara_el_mensaje_diario(corredor, telegram, clima, gemini):
    corredor(sesiones=(LUNES,))
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    assert asyncio.run(bot.handle_text(telegram, CHAT, "cansado", a_las(6, 40))) == bot.FATIGUE_RETRY_TEXT
    assert len(enviados(telegram)) == 1 and gemini["prompts"] == []


# --- Clima (AC-12, AC-13, AC-23, AC-24, AC-64) ---


def test_con_condicion_desfavorable_el_mensaje_lleva_la_alerta_y_el_dia_alternativo(
    corredor, telegram, clima, gemini, capsys
):
    corredor(sesiones=(LUNES,))
    clima["respuesta"] = pronostico(LLUVIA, CALOR, LLUVIA, BUENO)

    mensaje = flujo(telegram)

    assert "Alerta climática: probabilidad de lluvia de 80% en tu horario." in mensaje
    assert "Día alternativo sin condición desfavorable: jueves 8/10." in mensaje
    assert "?" not in mensaje
    (registro,) = registros(capsys, "decision_climatica")
    assert (registro["alerta"], registro["motivos"]) == (True, ["lluvia"])
    assert (registro["alternativa"], registro["lluvia"]) == ("2026-10-08", 80)


def test_sin_dia_alternativo_el_mensaje_lo_dice(corredor, telegram, clima, gemini, capsys):
    corredor(sesiones=(LUNES,))
    clima["respuesta"] = pronostico(CALOR, LLUVIA, CALOR, LLUVIA, BUENO)

    mensaje = flujo(telegram)

    assert "Alerta climática: temperatura de hasta 35 °C en tu horario." in mensaje
    assert SIN_ALTERNATIVA in mensaje and "Día alternativo" not in mensaje
    (registro,) = registros(capsys, "decision_climatica")
    assert registro["alerta"] is True and registro["alternativa"] is None


def test_sin_condicion_desfavorable_se_registra_la_decision_sin_alerta(
    corredor, telegram, clima, gemini, capsys
):
    corredor(sesiones=(LUNES,))
    assert "Alerta" not in flujo(telegram)
    (registro,) = registros(capsys, "decision_climatica")
    assert (registro["disponible"], registro["alerta"], registro["motivos"]) == (True, False, [])


@pytest.mark.parametrize(
    "fallo, motivo",
    [
        (httpx.ReadTimeout("sin respuesta"), "timeout"),
        (httpx.Response(429, json={}), "rate_limit"),
        (httpx.Response(503, text="error"), "error_del_servidor"),
    ],
)
def test_si_el_clima_falla_el_mensaje_se_entrega_con_el_texto_de_clima_no_disponible(
    corredor, telegram, clima, gemini, capsys, fallo, motivo
):
    corredor(sesiones=(CALIDAD,))
    clima["respuesta"] = fallo

    mensaje = flujo(telegram)

    assert CLIMA_NO_DISPONIBLE in mensaje and "Alerta" not in mensaje
    assert "Sesión de hoy: calidad." in mensaje and "Ritmo objetivo: 5:45 min/km." in mensaje
    salida = capsys.readouterr().out.strip().splitlines()
    eventos = {r["event"]: r for r in map(json.loads, salida)}
    assert eventos["external_error"]["servicio"] == "openweather"
    assert eventos["external_error"]["motivo"] == motivo
    assert eventos["decision_climatica"]["disponible"] is False


def test_si_el_pronostico_no_trae_datos_de_hoy_el_mensaje_dice_clima_no_disponible(
    corredor, telegram, clima, gemini
):
    corredor(sesiones=(LUNES,))
    martes = int(a_las(9, dia=LUNES + timedelta(days=1)).timestamp())
    clima["respuesta"] = httpx.Response(
        200, json={"list": [{"dt": martes, "main": {"temp": 20}, "pop": 0.1}]}
    )
    assert CLIMA_NO_DISPONIBLE in flujo(telegram)


# --- Valores vigentes del perfil (RF-18c, AC-71) ---


def test_el_mensaje_usa_la_ciudad_y_el_rango_horario_vigentes(client, corredor, telegram, clima, gemini):
    headers = corredor(sesiones=(LUNES,))
    cambio = {"city": "Mendoza", "window_start": "18:00", "window_end": "20:00"}
    assert client.put("/profile", json=cambio, headers=headers).status_code == 200
    # Con el rango nuevo, la lluvia de la mañana ya no cuenta y la de la tarde sí.
    clima["respuesta"] = httpx.Response(
        200,
        json={
            "list": [
                {"dt": int(a_las(9).timestamp()), "main": {"temp": 20}, "pop": 0.1},
                {"dt": int(a_las(18).timestamp()), "main": {"temp": 20}, "pop": 0.9},
            ]
        },
    )

    mensaje = flujo(telegram, respuesta="4")

    assert clima["ciudades"] == ["Mendoza"]
    assert "Horario sugerido: 18:00 a 20:00." in mensaje
    assert "probabilidad de lluvia de 90%" in mensaje


# --- Clima antes de la pregunta de fatiga (AC-23) ---


def test_el_clima_se_evalua_antes_de_preguntar_la_fatiga_y_no_se_vuelve_a_consultar(
    corredor, telegram, clima, gemini, conn
):
    corredor(sesiones=(LUNES,))
    clima["respuesta"] = pronostico(LLUVIA, BUENO)
    orden = []
    telegram.send_message.side_effect = lambda **kwargs: orden.append(("telegram", list(clima["ciudades"])))

    asyncio.run(bot.tick(telegram, a_las(6, 30)))

    assert orden == [("telegram", ["Rosario"])]  # al preguntar, el clima ya estaba consultado
    guardado = json.loads(conn.execute("SELECT weather FROM fatigue_checks").fetchone()[0])
    assert guardado == {
        "available": True,
        "alert": {
            "reasons": ["lluvia"],
            "rain_percent": 80,
            "temp_c": 20.0,
            "alternative": "2026-10-06",
        },
    }

    # El pronóstico cambia después de la pregunta: el mensaje usa la evaluación de la hora de envío.
    clima["respuesta"] = pronostico(BUENO, BUENO)
    asyncio.run(bot.handle_text(telegram, CHAT, "5", a_las(6, 40)))

    assert clima["ciudades"] == ["Rosario"]
    mensaje = telegram.send_message.call_args_list[-1].kwargs["text"]
    assert "probabilidad de lluvia de 80%" in mensaje and "martes 6/10" in mensaje


def test_sin_evaluacion_guardada_el_mensaje_dice_clima_no_disponible(corredor, telegram, clima, gemini, conn):
    corredor(sesiones=(LUNES,))
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    conn.execute("UPDATE fatigue_checks SET weather = NULL")
    conn.commit()

    asyncio.run(bot.handle_text(telegram, CHAT, "5", a_las(6, 40)))

    assert CLIMA_NO_DISPONIBLE in enviados(telegram)[-1][1]


def test_un_fallo_inesperado_al_evaluar_el_clima_descarta_la_pregunta_y_se_reintenta(
    corredor, telegram, clima, gemini, conn, monkeypatch, capsys
):
    corredor(sesiones=(LUNES,))
    original = bot.delivery.evaluate_weather

    def falla(check):
        raise RuntimeError("fallo inesperado")

    monkeypatch.setattr(bot.delivery, "evaluate_weather", falla)
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    assert enviados(telegram) == []
    assert conn.execute("SELECT COUNT(*) FROM fatigue_checks").fetchone()[0] == 0
    (registro,) = registros(capsys, "pregunta_de_fatiga_error")
    assert registro["tipo"] == "RuntimeError"

    monkeypatch.setattr(bot.delivery, "evaluate_weather", original)
    asyncio.run(bot.tick(telegram, a_las(6, 31)))
    assert enviados(telegram) == [(CHAT, bot.FATIGUE_QUESTION_TEXT)]


# --- Aislamiento entre usuarios ---


def test_cada_usuario_recibe_solo_su_sesion_en_su_chat(corredor, telegram, clima, gemini):
    corredor(sesiones=(CALIDAD,))
    corredor(email="mile@example.com", chat=2002, sesiones=(NOVATA,), perfil={**PERFIL, "city": "Salta"})
    asyncio.run(bot.tick(telegram, a_las(6, 30)))

    asyncio.run(bot.handle_text(telegram, 2002, "3", a_las(6, 40)))

    mensajes = [(c, t) for c, t in enviados(telegram) if t != bot.FATIGUE_QUESTION_TEXT]
    ((chat, mensaje),) = mensajes
    assert chat == 2002 and "3 x (0,4 km caminata + 0,4 km trote)" in mensaje
    assert "5:45" not in mensaje
    assert sorted(clima["ciudades"]) == ["Rosario", "Salta"]  # el clima de cada uno, con su ciudad


# --- Telegram ---


def test_si_telegram_falla_al_enviar_el_mensaje_se_registra(corredor, telegram, clima, gemini, capsys):
    corredor(sesiones=(LUNES,))
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    telegram.send_message.side_effect = TimedOut()

    asyncio.run(bot.handle_text(telegram, CHAT, "5", a_las(6, 40)))

    (registro,) = registros(capsys, "external_error")
    assert (registro["servicio"], registro["motivo"]) == ("telegram", "mensaje_diario")


def test_un_fallo_inesperado_en_un_vencimiento_no_frena_a_los_demas(
    corredor, telegram, clima, gemini, monkeypatch, capsys
):
    corredor(sesiones=(LUNES,))
    corredor(email="mile@example.com", chat=2002, sesiones=(LUNES,))
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    original = bot.delivery.prepare_message

    def falla_para_el_primero(check, fatiga):
        if check.chat_id == CHAT:
            raise RuntimeError("fallo inesperado")
        return original(check, fatiga)

    monkeypatch.setattr(bot.delivery, "prepare_message", falla_para_el_primero)
    asyncio.run(bot.tick(telegram, a_las(8, 30)))

    assert [c for c, t in enviados(telegram) if t != bot.FATIGUE_QUESTION_TEXT] == [2002]
    (registro,) = registros(capsys, "mensaje_diario_error")
    assert registro["tipo"] == "RuntimeError"


# --- RNF-01: 10 usuarios simultáneos ---


def test_diez_respuestas_simultaneas_se_atienden_a_la_vez(corredor, telegram, clima, gemini, monkeypatch):
    chats = [3000 + n for n in range(10)]
    for chat in chats:
        corredor(email=f"corredor{chat}@example.com", chat=chat, sesiones=(LUNES,))
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    redactar = writer._call_gemini
    monkeypatch.setattr(writer, "_call_gemini", lambda prompt: time.sleep(0.5) or redactar(prompt))

    async def todos():
        await asyncio.gather(*(bot.handle_text(telegram, c, "5", a_las(6, 40)) for c in chats))

    inicio = time.monotonic()
    asyncio.run(todos())
    duracion = time.monotonic() - inicio

    entregados = [c for c, t in enviados(telegram) if t != bot.FATIGUE_QUESTION_TEXT]
    assert sorted(entregados) == chats
    # En fila serían 5 segundos (10 x 0,5 s); a la vez, muy por debajo de los 10 de RNF-01.
    assert duracion < 3


def test_el_bot_atiende_10_updates_a_la_vez(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    assert bot.build_application().update_processor.max_concurrent_updates == 10
