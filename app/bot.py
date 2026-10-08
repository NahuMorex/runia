"""Bot de Telegram (polling), que corre en el mismo proceso que la API."""

import logging

from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from app import config, db
from app.linking import LinkResult, redeem_link_code
from app.log import log_event

START_TEXT = (
    "Hola, soy Runnia. Para vincular este chat con tu cuenta, generá un código "
    "desde la API y enviame: /vincular CODIGO"
)
LINK_USAGE_TEXT = "Enviame el código así: /vincular CODIGO"
LINK_REPLIES = {
    LinkResult.LINKED: "Listo, este chat quedó vinculado a tu cuenta.",
    LinkResult.INVALID_CODE: "El código es inválido, ya fue usado o venció. Generá uno nuevo.",
    LinkResult.ALREADY_LINKED: (
        "Este chat o esa cuenta ya tienen un vínculo, y no se puede cambiar."
    ),
}


def link_chat(chat_id: int, args: list[str]) -> str:
    """Canjea el código de vinculación y devuelve el texto de respuesta."""
    if len(args) != 1:
        return LINK_USAGE_TEXT
    conn = db.connect()
    try:
        return LINK_REPLIES[redeem_link_code(conn, args[0], chat_id)]
    finally:
        conn.close()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_TEXT)


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = link_chat(update.effective_chat.id, context.args or [])
    await update.effective_message.reply_text(reply)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("telegram_error", level=logging.ERROR, error=repr(context.error))


def build_application() -> Application:
    application = Application.builder().token(config.telegram_bot_token()).build()
    # Solo chats privados: un grupo no puede quedar vinculado a una cuenta.
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start_command, filters=private))
    application.add_handler(CommandHandler("vincular", link_command, filters=private))
    application.add_error_handler(on_error)
    return application


async def start_bot() -> Application:
    application = build_application()
    try:
        await application.initialize()
    except InvalidToken:
        # "from None": el error original incluye el token y no debe llegar a los logs.
        raise RuntimeError("Telegram rechazó el TELEGRAM_BOT_TOKEN") from None
    await application.start()
    await application.updater.start_polling()
    log_event("bot_start")
    return application


async def stop_bot(application: Application) -> None:
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log_event("bot_stop")
