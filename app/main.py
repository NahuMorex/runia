"""Punto de entrada: API de Runnia."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth import router as auth_router
from app.db import init_db
from app.log import log_event
from app.profile import router as profile_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log_event("app_start")
    yield
    log_event("app_stop")


app = FastAPI(title="Runnia", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(profile_router)
