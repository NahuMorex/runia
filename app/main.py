"""Punto de entrada: API de Runia y bot de Telegram en el mismo proceso."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth import router as auth_router
from app.bot import start_bot, stop_bot
from app.db import init_db
from app.linking import router as linking_router
from app.log import log_event
from app.plan import router as plan_router
from app.profile import router as profile_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log_event("app_start")
    # Sin token la API levanta sola (tests, desarrollo sin Telegram) y lo deja registrado.
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        app.state.bot = await start_bot()
    else:
        app.state.bot = None
        log_event("bot_disabled", level=logging.WARNING, motivo="falta TELEGRAM_BOT_TOKEN")
    yield
    if app.state.bot is not None:
        await stop_bot(app.state.bot)
    log_event("app_stop")


app = FastAPI(title="Runia", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(linking_router)
app.include_router(plan_router)
