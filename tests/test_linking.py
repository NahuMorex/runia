import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot
from app.linking import (
    CODE_ALPHABET,
    CODE_LENGTH,
    LinkResult,
    create_link_code,
    redeem_link_code,
)

CHAT = 1001
OTRO_CHAT = 2002


def pedir_codigo(client, headers):
    r = client.post("/telegram/link-code", headers=headers)
    assert r.status_code == 201
    return r.json()["code"]


def chat_de(conn, email):
    return conn.execute("SELECT telegram_chat_id FROM users WHERE email = ?", (email,)).fetchone()[0]


def id_de(conn, email):
    return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]


def test_el_endpoint_devuelve_un_codigo_que_vence_en_15_minutos(client, auth):
    r = client.post("/telegram/link-code", headers=auth())
    assert r.status_code == 201
    assert r.json()["expires_in_minutes"] == 15
    codigo = r.json()["code"]
    assert len(codigo) == CODE_LENGTH and set(codigo) <= set(CODE_ALPHABET)


def test_pedir_un_codigo_exige_token(client):
    assert client.post("/telegram/link-code").status_code == 401


# AC-44
def test_un_codigo_valido_vincula_el_chat_y_no_se_puede_reusar(client, auth, conn):
    codigo = pedir_codigo(client, auth())

    assert redeem_link_code(conn, codigo, CHAT) is LinkResult.LINKED
    assert chat_de(conn, "pepe@example.com") == CHAT
    assert redeem_link_code(conn, codigo, OTRO_CHAT) is LinkResult.INVALID_CODE
    assert chat_de(conn, "pepe@example.com") == CHAT


# AC-44: dentro de los 15 minutos, incluido el límite.
def test_un_codigo_de_15_minutos_justos_todavia_sirve(client, auth, conn):
    auth()
    hace_15 = datetime.now(timezone.utc) - timedelta(minutes=15)
    ahora = hace_15 + timedelta(minutes=15)
    codigo = create_link_code(conn, id_de(conn, "pepe@example.com"), now=hace_15)
    assert redeem_link_code(conn, codigo, CHAT, now=ahora) is LinkResult.LINKED


def test_el_codigo_se_acepta_en_minusculas_y_con_espacios(client, auth, conn):
    codigo = pedir_codigo(client, auth())
    assert redeem_link_code(conn, f" {codigo.lower()} ", CHAT) is LinkResult.LINKED


# AC-45
def test_un_codigo_inexistente_se_rechaza(client, auth, conn):
    auth()
    assert redeem_link_code(conn, "NOEXISTE", CHAT) is LinkResult.INVALID_CODE
    assert chat_de(conn, "pepe@example.com") is None


# AC-45
def test_un_codigo_con_mas_de_15_minutos_se_rechaza(client, auth, conn):
    auth()
    viejo = datetime.now(timezone.utc) - timedelta(minutes=15, seconds=1)
    codigo = create_link_code(conn, id_de(conn, "pepe@example.com"), now=viejo)

    assert redeem_link_code(conn, codigo, CHAT) is LinkResult.INVALID_CODE
    assert chat_de(conn, "pepe@example.com") is None


# AC-46: el chat ya está vinculado a otra cuenta.
def test_un_chat_ya_vinculado_no_se_vincula_a_otra_cuenta(client, auth, conn):
    redeem_link_code(conn, pedir_codigo(client, auth()), CHAT)
    codigo_de_mich = pedir_codigo(client, auth("mich@example.com"))

    assert redeem_link_code(conn, codigo_de_mich, CHAT) is LinkResult.ALREADY_LINKED
    assert chat_de(conn, "pepe@example.com") == CHAT
    assert chat_de(conn, "mich@example.com") is None


# AC-46: la cuenta ya tiene un chat vinculado.
def test_una_cuenta_ya_vinculada_no_admite_un_segundo_chat(client, auth, conn):
    headers = auth()
    primero, segundo = pedir_codigo(client, headers), pedir_codigo(client, headers)
    redeem_link_code(conn, primero, CHAT)

    assert redeem_link_code(conn, segundo, OTRO_CHAT) is LinkResult.ALREADY_LINKED
    assert chat_de(conn, "pepe@example.com") == CHAT


# AC-46
def test_una_cuenta_ya_vinculada_no_puede_pedir_otro_codigo(client, auth, conn):
    headers = auth()
    redeem_link_code(conn, pedir_codigo(client, headers), CHAT)
    assert client.post("/telegram/link-code", headers=headers).status_code == 409


def enviar_al_bot(handler, chat_id, args):
    """Invoca un handler del bot con un update simulado y devuelve el texto respondido."""
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    asyncio.run(handler(update, SimpleNamespace(args=args)))
    return reply_text.await_args.args[0]


# AC-44
def test_el_bot_vincula_el_chat_con_el_comando_vincular(client, auth, conn):
    codigo = pedir_codigo(client, auth())
    respuesta = enviar_al_bot(bot.link_command, CHAT, [codigo])
    assert respuesta == bot.LINK_REPLIES[LinkResult.LINKED]
    assert chat_de(conn, "pepe@example.com") == CHAT


# AC-45
def test_el_bot_rechaza_un_codigo_invalido(client, auth, conn):
    auth()
    respuesta = enviar_al_bot(bot.link_command, CHAT, ["NOEXISTE"])
    assert respuesta == bot.LINK_REPLIES[LinkResult.INVALID_CODE]
    assert chat_de(conn, "pepe@example.com") is None


# AC-46
def test_el_bot_rechaza_un_segundo_vinculo(client, auth, conn):
    enviar_al_bot(bot.link_command, CHAT, [pedir_codigo(client, auth())])
    codigo_de_mich = pedir_codigo(client, auth("mich@example.com"))
    respuesta = enviar_al_bot(bot.link_command, CHAT, [codigo_de_mich])
    assert respuesta == bot.LINK_REPLIES[LinkResult.ALREADY_LINKED]
    assert chat_de(conn, "mich@example.com") is None


@pytest.mark.parametrize("args", [[], ["UNO", "DOS"]])
def test_el_bot_explica_el_uso_si_no_recibe_un_unico_codigo(client, args):
    assert enviar_al_bot(bot.link_command, CHAT, args) == bot.LINK_USAGE_TEXT


def test_el_bot_registra_los_comandos_solo_para_chats_privados(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    handlers = bot.build_application().handlers[0]
    assert {c for h in handlers for c in h.commands} == {"start", "vincular"}
    assert all(h.filters is not None for h in handlers)
