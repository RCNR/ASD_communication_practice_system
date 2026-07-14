from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import admin, auth, chat, health, student
from app.core.config import settings
from app.core.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

app.include_router(health.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(auth.router)
app.include_router(student.router)
app.include_router(admin.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
def index(request: Request):
    if request.session.get("participant_code"):
        return RedirectResponse(url="/session", status_code=303)
    return RedirectResponse(url="/home", status_code=303)
