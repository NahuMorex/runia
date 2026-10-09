"""Bot de Telegram (polling), que corre en el mismo proceso que la API."""

import asyncio
import logging
import time
from datetime import datetime

from telegram import Bot, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app import config, db, delivery, fatigue
from app.daily import word_count
from app.fatigue import Check, ReplyStatus
from app.linking import LinkResult, redeem_link_code
from app.log import log_event

START_TEXT = (
    "Hola, soy Runia. Para vincular este chat con tu cuenta, generá un código "
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

# RF-15
FATIGUE_QUESTION_TEXT = (
    "Hoy tenés sesión. ¿Cómo está tu fatiga, del 1 al 10? Respondé solo con el número."
)
# AC-75
FATIGUE_RETRY_TEXT = "No entendí tu respuesta. Decime tu fatiga con un número entero del 1 al 10."
# Cada cuánto se revisa si a alguien le llegó la hora de envío o se le venció la espera.
TICK_SECONDS = 30
# RNF-01: 10 usuarios simultáneos.
CONCURRENT_UPDATES = 10


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


async def deliver_daily(bot: Bot, check: Check, fatigue_level: int | None) -> None:
    """Arma y envía el mensaje diario (RF-08); `fatigue_level` es None si no respondió."""
    started = time.monotonic()
    # El clima y Gemini son llamadas bloqueantes: van en un hilo para no frenar al resto (RNF-01).
    text = await asyncio.to_thread(delivery.prepare_message, check, fatigue_level)
    if text is None:
        return
    try:
        await bot.send_message(chat_id=check.chat_id, text=text)
    except TelegramError as error:
        log_event(
            "external_error",
            level=logging.ERROR,
            servicio="telegram",
            motivo="mensaje_diario",
            tipo=type(error).__name__,
            user_id=check.user_id,
        )
        return
    log_event(
        "mensaje_diario",
        user_id=check.user_id,
        fecha=check.day.isoformat(),
        fatiga=fatigue_level,
        palabras=word_count(text),
        duracion_ms=round((time.monotonic() - started) * 1000),
    )


async def ask_fatigue(bot: Bot, now: datetime | None = None) -> None:
    """Pregunta la fatiga a quienes les llegó la hora de envío y tienen sesión hoy (AC-47)."""
    conn = db.connect()
    try:
        for check in fatigue.open_due_checks(conn, now or fatigue.now_utc()):
            try:
                await bot.send_message(chat_id=check.chat_id, text=FATIGUE_QUESTION_TEXT)
            except TelegramError as error:
                # Sin pregunta enviada no hay espera: se descarta y se reintenta en la próxima vuelta.
                fatigue.discard_check(conn, check)
                log_event(
                    "external_error",
                    level=logging.ERROR,
                    servicio="telegram",
                    motivo="pregunta_de_fatiga",
                    tipo=type(error).__name__,
                    user_id=check.user_id,
                )
            else:
                log_event("pregunta_de_fatiga", user_id=check.user_id, fecha=check.day.isoformat())
    finally:
        conn.close()


async def expire_fatigue(bot: Bot, now: datetime | None = None) -> None:
    """Cierra las preguntas con 2 horas sin respuesta y sigue sin ajustar por fatiga (AC-47)."""
    conn = db.connect()
    try:
        expired = fatigue.close_expired_checks(conn, now or fatigue.now_utc())
    finally:
        conn.close()
    results = await asyncio.gather(
        *(deliver_daily(bot, check, None) for check in expired), return_exceptions=True
    )
    for check, result in zip(expired, results):
        if isinstance(result, Exception):
            log_event(
                "mensaje_diario_error",
                level=logging.ERROR,
                tipo=type(result).__name__,
                user_id=check.user_id,
            )


async def handle_text(bot: Bot, chat_id: int, text: str, now: datetime | None = None) -> str | None:
    """Toma un mensaje de texto como respuesta de fatiga; devuelve qué contestarle, si algo."""
    conn = db.connect()
    try:
        reply = fatigue.record_reply(conn, chat_id, text, now or fatigue.now_utc())
    finally:
        conn.close()
    if reply.status is ReplyStatus.INVALID:
        return FATIGUE_RETRY_TEXT
    if reply.status is ReplyStatus.ACCEPTED:
        await deliver_daily(bot, reply.check, reply.fatigue)
    return None


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    reply = await handle_text(context.bot, update.effective_chat.id, message.text or "")
    if reply:
        await message.reply_text(reply)


async def tick(bot: Bot, now: datetime | None = None) -> None:
    await expire_fatigue(bot, now)
    await ask_fatigue(bot, now)


async def run_ticker(bot: Bot) -> None:
    """Revisa cada TICK_SECONDS; un error en una vuelta no frena las siguientes."""
    while True:
        try:
            await tick(bot)
        except Exception as error:
            log_event("tick_error", level=logging.ERROR, tipo=type(error).__name__)
        await asyncio.sleep(TICK_SECONDS)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("telegram_error", level=logging.ERROR, error=repr(context.error))


def build_application() -> Application:
    # RNF-01: las respuestas de varios usuarios se atienden a la vez, no en fila.
    application = (
        Application.builder()
        .token(config.telegram_bot_token())
        .concurrent_updates(CONCURRENT_UPDATES)
        .build()
    )
    # Solo chats privados: un grupo no puede quedar vinculado a una cuenta.
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start_command, filters=private))
    application.add_handler(CommandHandler("vincular", link_command, filters=private))
    application.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, text_message))
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
    application.bot_data["ticker"] = asyncio.create_task(run_ticker(application.bot))
    log_event("bot_start")
    return application


async def stop_bot(application: Application) -> None:
    ticker = application.bot_data.pop("ticker", None)
    if ticker is not None:
        ticker.cancel()
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log_event("bot_stop")
