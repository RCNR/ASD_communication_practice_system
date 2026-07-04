import hashlib

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.participant import Participant

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    participant_code: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    participant = db.query(Participant).filter_by(participant_code=participant_code).first()
    password_hash = hashlib.sha256(password.encode()).hexdigest()

    if not participant or participant.password_hash != password_hash:
        return templates.TemplateResponse(
            request, "login.html", {"error": "참여자 코드 또는 비밀번호가 올바르지 않습니다."}
        )

    if participant.status != "active":
        return templates.TemplateResponse(
            request, "login.html", {"error": "이 계정은 현재 이용할 수 없습니다. 연구자에게 문의해 주세요."}
        )

    request.session["participant_code"] = participant.participant_code
    return RedirectResponse(url="/session", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
