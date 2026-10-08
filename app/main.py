"""Punto de entrada: API de Runnia."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.log import log_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log_event("app_start")
    yield
    log_event("app_stop")


app = FastAPI(title="Runnia", lifespan=lifespan)
