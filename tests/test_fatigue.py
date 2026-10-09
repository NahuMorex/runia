import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import Chat, Message, MessageEntity, Update
from telegram.error import TimedOut
from telegram.ext import MessageHandler

from app import bot, fatigue
from app.fatigue import ReplyStatus, close_expired_checks, open_due_checks, parse_fatigue, record_reply

BA = ZoneInfo("America/Argentina/Buenos_Aires")
LUNES = date(2026, 10, 5)
MARTES = LUNES + timedelta(days=1)
CHAT = 1001
OTRO_CHAT = 2002
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


def a_las(hora, minuto=0, dia=LUNES):
    return datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=BA)


def registros(capsys):
    return [json.loads(linea) for linea in capsys.readouterr().out.strip().splitlines()]


@pytest.fixture
def corredor(client, auth, conn):
    """Crea un usuario con perfil, chat vinculado y sesiones planificadas; devuelve sus headers."""

    def _corredor(email="pepe@example.com", chat=CHAT, sesiones=(LUNES,), perfil=PERFIL):
        headers = auth(email)
        assert client.put("/profile", json=perfil, headers=headers).status_code == 200
        user_id = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
        conn.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat, user_id))
        plan_id = conn.execute(
            "INSERT INTO plans (user_id, week_start, planned_km, created_at) VALUES (?, ?, 5, ?)",
            (user_id, LUNES.isoformat(), a_las(0).isoformat()),
        ).lastrowid
        for dia in sesiones:
            conn.execute(
                "INSERT INTO sessions (plan_id, user_id, date, kind, jog_km, detail) "
                "VALUES (?, ?, ?, 'suave', 5, ?)",
                (plan_id, user_id, dia.isoformat(), '{"blocks": [{"kind": "trote", "km": 5}]}'),
            )
        conn.commit()
        return headers

    return _corredor


@pytest.fixture
def telegram():
    """Bot falso que guarda los mensajes enviados; nunca se sale a la red."""
    return SimpleNamespace(send_message=AsyncMock())


@pytest.fixture
def entregas(monkeypatch):
    """Reemplaza el envío del mensaje diario y guarda con qué fatiga se pidió."""
    llamadas = []

    async def falso(bot_, check, fatiga):
        llamadas.append((check.chat_id, check.day, fatiga))

    monkeypatch.setattr(bot, "deliver_daily", falso)
    return llamadas


def enviados(telegram):
    return [(c.kwargs["chat_id"], c.kwargs["text"]) for c in telegram.send_message.call_args_list]


def fila(conn):
    return conn.execute("SELECT fatigue, closed_at FROM fatigue_checks").fetchone()


# --- Respuesta válida (AC-75) ---


@pytest.mark.parametrize("texto, fatiga", [("1", 1), ("7", 7), ("10", 10), (" 8 \n", 8)])
def test_un_entero_de_1_a_10_es_una_fatiga_valida(texto, fatiga):
    assert parse_fatigue(texto) == fatiga


@pytest.mark.parametrize(
    "texto", ["0", "11", "100", "-3", "8.5", "8,5", "8/10", "ocho", "un 8", "08", "", "  ", "7 8"]
)
def test_cualquier_otra_cosa_no_es_una_fatiga_valida(texto):
    assert parse_fatigue(texto) is None


# --- A quién se le pregunta (RF-15, AC-47) ---


def test_a_la_hora_de_envio_se_abre_la_pregunta_del_dia(corredor, conn):
    corredor()
    (pregunta,) = open_due_checks(conn, a_las(6, 30))
    assert (pregunta.chat_id, pregunta.day, pregunta.asked_at) == (CHAT, LUNES, a_las(6, 30))


def test_antes_de_la_hora_de_envio_no_se_pregunta(corredor, conn):
    corredor()
    assert open_due_checks(conn, a_las(6, 29)) == []


def test_no_se_pregunta_dos_veces_el_mismo_dia(corredor, conn):
    corredor()
    assert len(open_due_checks(conn, a_las(6, 30))) == 1
    assert open_due_checks(conn, a_las(6, 31)) == []
    record_reply(conn, CHAT, "5", a_las(6, 40))
    assert open_due_checks(conn, a_las(6, 45)) == []


def test_un_dia_sin_sesion_planificada_no_se_pregunta(corredor, conn):
    corredor(sesiones=(MARTES,))
    assert open_due_checks(conn, a_las(6, 30)) == []
    assert len(open_due_checks(conn, a_las(6, 30, MARTES))) == 1


def test_sin_plan_o_sin_chat_vinculado_no_se_pregunta(corredor, conn):
    corredor(sesiones=())
    corredor(email="mich@example.com", chat=None)
    assert open_due_checks(conn, a_las(6, 30)) == []


def test_si_la_hora_de_envio_ya_paso_se_pregunta_mientras_dure_el_rango_horario(corredor, conn):
    corredor()
    assert len(open_due_checks(conn, a_las(8, 59))) == 1


def test_terminado_el_rango_horario_ya_no_se_pregunta(corredor, conn):
    corredor()
    assert open_due_checks(conn, a_las(9, 0)) == []
    assert open_due_checks(conn, a_las(23, 0)) == []


def test_cada_usuario_recibe_su_pregunta_a_su_hora(corredor, conn):
    corredor()
    corredor(email="mich@example.com", chat=OTRO_CHAT, perfil={**PERFIL, "send_time": "08:00"})
    assert [c.chat_id for c in open_due_checks(conn, a_las(6, 30))] == [CHAT]
    assert [c.chat_id for c in open_due_checks(conn, a_las(8, 0))] == [OTRO_CHAT]


# AC-71: la hora de envío es la vigente en el perfil.
def test_la_pregunta_usa_la_hora_de_envio_vigente(client, corredor, conn):
    headers = corredor()
    assert client.put("/profile", json={"send_time": "08:15"}, headers=headers).status_code == 200
    assert open_due_checks(conn, a_las(6, 30)) == []
    assert len(open_due_checks(conn, a_las(8, 15))) == 1


# --- Respuesta del usuario (AC-47, AC-75) ---


def test_una_respuesta_valida_cierra_la_pregunta_con_la_fatiga(corredor, conn):
    corredor()
    (pregunta,) = open_due_checks(conn, a_las(6, 30))

    respuesta = record_reply(conn, CHAT, "8", a_las(6, 45))

    assert (respuesta.status, respuesta.fatigue) == (ReplyStatus.ACCEPTED, 8)
    assert respuesta.check == pregunta
    assert tuple(fila(conn)) == (8, a_las(6, 45).isoformat())


def test_una_respuesta_invalida_deja_la_pregunta_abierta(corredor, conn):
    corredor()
    open_due_checks(conn, a_las(6, 30))

    assert record_reply(conn, CHAT, "bastante cansado", a_las(6, 45)).status is ReplyStatus.INVALID
    assert tuple(fila(conn)) == (None, None)
    assert record_reply(conn, CHAT, "6", a_las(6, 50)).status is ReplyStatus.ACCEPTED


def test_sin_pregunta_pendiente_un_mensaje_no_es_una_respuesta(corredor, conn):
    corredor()
    assert record_reply(conn, CHAT, "5", a_las(6, 0)).status is ReplyStatus.NO_PENDING
    open_due_checks(conn, a_las(6, 30))
    record_reply(conn, CHAT, "5", a_las(6, 40))
    assert record_reply(conn, CHAT, "9", a_las(6, 41)).status is ReplyStatus.NO_PENDING
    assert fila(conn)["fatigue"] == 5


def test_la_respuesta_de_un_chat_no_cierra_la_pregunta_de_otro(corredor, conn):
    corredor()
    corredor(email="mich@example.com", chat=OTRO_CHAT)
    open_due_checks(conn, a_las(6, 30))

    respuesta = record_reply(conn, OTRO_CHAT, "3", a_las(6, 40))

    assert respuesta.check.chat_id == OTRO_CHAT
    filas = conn.execute("SELECT fatigue FROM fatigue_checks ORDER BY user_id").fetchall()
    assert [f["fatigue"] for f in filas] == [None, 3]
    assert record_reply(conn, 9999, "3", a_las(6, 40)).status is ReplyStatus.NO_PENDING


# --- Espera máxima de 2 horas (AC-47, AC-75) ---


def test_la_espera_vence_a_las_2_horas_de_la_primera_pregunta(corredor, conn):
    corredor()
    (pregunta,) = open_due_checks(conn, a_las(6, 30))

    assert close_expired_checks(conn, a_las(8, 29)) == []
    assert close_expired_checks(conn, a_las(8, 30)) == [pregunta]
    assert tuple(fila(conn)) == (None, a_las(8, 30).isoformat())
    assert close_expired_checks(conn, a_las(8, 31)) == []


# AC-75: volver a preguntar no reinicia la espera.
def test_una_respuesta_invalida_no_reinicia_la_espera(corredor, conn):
    corredor()
    open_due_checks(conn, a_las(6, 30))
    record_reply(conn, CHAT, "mmm", a_las(8, 20))
    assert len(close_expired_checks(conn, a_las(8, 30))) == 1


def test_una_respuesta_despues_de_las_2_horas_ya_no_cuenta(corredor, conn):
    corredor()
    open_due_checks(conn, a_las(6, 30))
    assert record_reply(conn, CHAT, "9", a_las(8, 30)).status is ReplyStatus.NO_PENDING
    assert len(close_expired_checks(conn, a_las(8, 30))) == 1
    assert fila(conn)["fatigue"] is None


def test_una_pregunta_respondida_no_vence(corredor, conn):
    corredor()
    open_due_checks(conn, a_las(6, 30))
    record_reply(conn, CHAT, "4", a_las(7, 0))
    assert close_expired_checks(conn, a_las(9, 0)) == []
    assert fila(conn)["fatigue"] == 4


# --- Flujo en el bot ---


def test_el_bot_pregunta_la_fatiga_a_la_hora_de_envio(corredor, telegram, capsys):
    corredor()
    asyncio.run(bot.tick(telegram, a_las(6, 29)))
    assert enviados(telegram) == []

    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    asyncio.run(bot.tick(telegram, a_las(6, 31)))

    assert enviados(telegram) == [(CHAT, bot.FATIGUE_QUESTION_TEXT)]
    assert "1 al 10" in bot.FATIGUE_QUESTION_TEXT
    (registro,) = [r for r in registros(capsys) if r["event"] == "pregunta_de_fatiga"]
    assert registro["fecha"] == "2026-10-05"


def test_si_telegram_falla_al_preguntar_se_registra_y_se_reintenta(corredor, telegram, conn, capsys):
    corredor()
    telegram.send_message.side_effect = TimedOut()
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    assert conn.execute("SELECT COUNT(*) FROM fatigue_checks").fetchone()[0] == 0
    (registro,) = [r for r in registros(capsys) if r["event"] == "external_error"]
    assert (registro["servicio"], registro["tipo"]) == ("telegram", "TimedOut")

    telegram.send_message.side_effect = None
    asyncio.run(bot.tick(telegram, a_las(6, 31)))
    assert conn.execute("SELECT asked_at FROM fatigue_checks").fetchone()[0] == a_las(6, 31).isoformat()


# AC-75
def test_ante_una_respuesta_invalida_el_bot_vuelve_a_preguntar_sin_emitir_el_mensaje(
    corredor, telegram, entregas
):
    corredor()
    asyncio.run(bot.tick(telegram, a_las(6, 30)))

    respuesta = asyncio.run(bot.handle_text(telegram, CHAT, "muy cansado", a_las(6, 40)))

    assert respuesta == bot.FATIGUE_RETRY_TEXT and "1 al 10" in respuesta
    assert entregas == []


# AC-47
def test_con_una_respuesta_valida_el_bot_sigue_con_el_mensaje_diario(corredor, telegram, entregas):
    corredor()
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    asyncio.run(bot.handle_text(telegram, CHAT, "nada", a_las(6, 35)))

    assert asyncio.run(bot.handle_text(telegram, CHAT, "8", a_las(6, 40))) is None
    assert entregas == [(CHAT, LUNES, 8)]
    asyncio.run(bot.tick(telegram, a_las(8, 30)))
    assert entregas == [(CHAT, LUNES, 8)]


# AC-47: pasadas 2 horas sin respuesta, sigue sin ajustar por fatiga.
def test_a_las_2_horas_sin_respuesta_el_bot_sigue_sin_fatiga(corredor, telegram, entregas):
    corredor()
    asyncio.run(bot.tick(telegram, a_las(6, 30)))
    asyncio.run(bot.handle_text(telegram, CHAT, "después te digo", a_las(7, 0)))
    asyncio.run(bot.tick(telegram, a_las(8, 29)))
    assert entregas == []

    asyncio.run(bot.tick(telegram, a_las(8, 30)))
    asyncio.run(bot.tick(telegram, a_las(8, 31)))

    assert entregas == [(CHAT, LUNES, None)]
    assert asyncio.run(bot.handle_text(telegram, CHAT, "9", a_las(8, 32))) is None
    assert entregas == [(CHAT, LUNES, None)]


def test_un_mensaje_sin_pregunta_pendiente_no_tiene_respuesta(corredor, telegram, entregas):
    corredor()
    assert asyncio.run(bot.handle_text(telegram, CHAT, "hola", a_las(6, 0))) is None
    assert entregas == []


def test_el_handler_de_texto_responde_en_el_chat(corredor, telegram, conn, entregas):
    corredor()
    # El handler usa la hora real: la pregunta se abre a mano, recién hecha.
    conn.execute(
        "INSERT INTO fatigue_checks (user_id, date, asked_at) SELECT id, ?, ? FROM users",
        (LUNES.isoformat(), fatigue.now_utc().isoformat()),
    )
    conn.commit()
    reply_text = AsyncMock()

    def update(texto):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT),
            effective_message=SimpleNamespace(text=texto, reply_text=reply_text),
        )

    asyncio.run(bot.text_message(update("ocho"), SimpleNamespace(bot=telegram)))
    reply_text.assert_awaited_once_with(bot.FATIGUE_RETRY_TEXT)
    asyncio.run(bot.text_message(update("8"), SimpleNamespace(bot=telegram)))
    reply_text.assert_awaited_once()
    assert entregas == [(CHAT, LUNES, 8)]


def test_el_bot_escucha_texto_que_no_es_comando_solo_en_chats_privados(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    handlers = bot.build_application().handlers[0]
    (handler,) = [h for h in handlers if isinstance(h, MessageHandler)]
    assert handler.callback is bot.text_message

    def acepta(texto, tipo, entidades=()):
        mensaje = Message(1, a_las(6, 30), Chat(CHAT, tipo), text=texto, entities=entidades)
        return bool(handler.check_update(Update(1, message=mensaje)))

    comando = (MessageEntity(MessageEntity.BOT_COMMAND, 0, 9),)
    assert acepta("8", "private")
    assert not acepta("8", "group")
    assert not acepta("/vincular ABC", "private", comando)


def test_un_error_en_una_vuelta_no_frena_las_siguientes(monkeypatch, telegram, capsys):
    vueltas = []

    async def falso(bot_):
        vueltas.append(1)
        if len(vueltas) == 1:
            raise RuntimeError("falla de una vuelta")
        if len(vueltas) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "tick", falso)
    monkeypatch.setattr(bot, "TICK_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run_ticker(telegram))
    assert len(vueltas) == 3
    (registro,) = registros(capsys)
    assert (registro["event"], registro["tipo"]) == ("tick_error", "RuntimeError")
