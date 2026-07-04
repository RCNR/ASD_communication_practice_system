from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import chat, health
from app.core.config import settings

app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

app.include_router(health.router, prefix="/api")
app.include_router(chat.router, prefix="/api")

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
