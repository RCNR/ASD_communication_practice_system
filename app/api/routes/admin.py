from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.item import Item
from app.models.participant import Participant
from app.models.session import StudySession
from app.models.trial_response import TrialResponse
from app.services.item_import_service import parse_item_file, upsert_items
from app.services.session_service import PHASE_ORDER, get_latest_hint_message

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _require_admin(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


@router.get("/login")
def admin_login_form(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@router.post("/login")
def admin_login_submit(request: Request, password: str = Form(...)):
    if password != settings.ADMIN_PASSWORD:
        return templates.TemplateResponse(
            request, "admin_login.html", {"error": "비밀번호가 올바르지 않습니다."}
        )
    request.session["is_admin"] = True
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/logout")
def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("")
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    rows = []
    for participant in db.query(Participant).order_by(Participant.participant_code).all():
        session_counts = {
            phase: db.query(StudySession)
            .filter_by(participant_code=participant.participant_code, phase=phase, status="completed")
            .count()
            for phase in PHASE_ORDER
        }
        safety_flag_count = (
            db.query(TrialResponse)
            .join(StudySession, TrialResponse.session_id == StudySession.id)
            .filter(StudySession.participant_code == participant.participant_code)
            .filter(TrialResponse.safety_flag.isnot(None))
            .count()
        )
        rows.append(
            {
                "participant": participant,
                "session_counts": session_counts,
                "safety_flag_count": safety_flag_count,
            }
        )

    return templates.TemplateResponse(request, "admin_dashboard.html", {"rows": rows})


@router.get("/participants/new")
def admin_participant_new_form(request: Request):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(request, "admin_participant_new.html", {"error": None})


@router.post("/participants/new")
def admin_participant_new_submit(
    request: Request,
    participant_code: str = Form(...),
    password: str = Form(...),
    baseline_length: int = Form(...),
    db: Session = Depends(get_db),
):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    existing = db.query(Participant).filter_by(participant_code=participant_code).first()
    if existing:
        return templates.TemplateResponse(
            request,
            "admin_participant_new.html",
            {"error": f"참여자 코드 '{participant_code}'는 이미 존재합니다."},
        )

    db.add(
        Participant(
            participant_code=participant_code,
            password_hash=hashlib.sha256(password.encode()).hexdigest(),
            baseline_length=baseline_length,
            current_phase="baseline",
            status="active",
        )
    )
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/participants/{participant_code}/edit")
def admin_participant_edit_form(request: Request, participant_code: str, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participant = db.query(Participant).filter_by(participant_code=participant_code).first()
    if not participant:
        return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse(
        request, "admin_participant_edit.html", {"participant": participant, "error": None}
    )


@router.post("/participants/{participant_code}/edit")
def admin_participant_edit_submit(
    request: Request,
    participant_code: str,
    baseline_length: int = Form(...),
    current_phase: str = Form(...),
    status: str = Form(...),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participant = db.query(Participant).filter_by(participant_code=participant_code).first()
    if not participant:
        return RedirectResponse(url="/admin", status_code=303)

    if current_phase not in PHASE_ORDER:
        return templates.TemplateResponse(
            request,
            "admin_participant_edit.html",
            {"participant": participant, "error": "올바르지 않은 단계입니다."},
        )
    if status not in ("active", "paused", "dropped"):
        return templates.TemplateResponse(
            request,
            "admin_participant_edit.html",
            {"participant": participant, "error": "올바르지 않은 상태입니다."},
        )

    participant.baseline_length = baseline_length
    participant.current_phase = current_phase
    participant.status = status
    if new_password:
        participant.password_hash = hashlib.sha256(new_password.encode()).hexdigest()
    db.commit()

    return RedirectResponse(url="/admin", status_code=303)


@router.get("/participants/{participant_code}")
def admin_participant_detail(request: Request, participant_code: str, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participant = db.query(Participant).filter_by(participant_code=participant_code).first()
    if not participant:
        return RedirectResponse(url="/admin", status_code=303)

    trials = (
        db.query(TrialResponse, StudySession, Item)
        .join(StudySession, TrialResponse.session_id == StudySession.id)
        .join(Item, TrialResponse.item_id == Item.item_id)
        .filter(StudySession.participant_code == participant_code)
        .order_by(StudySession.phase, StudySession.session_number, TrialResponse.item_order)
        .all()
    )

    trial_rows = []
    for trial, study_session, item in trials:
        trial_rows.append(
            {
                "phase": study_session.phase,
                "session_number": study_session.session_number,
                "item_order": trial.item_order,
                "item_text": item.item_text,
                "first_response": trial.first_response,
                "hint1": get_latest_hint_message(db, trial.id, 1),
                "revised_response_1": trial.revised_response_1,
                "hint2": get_latest_hint_message(db, trial.id, 2),
                "revised_response_2": trial.revised_response_2,
                "example_used": trial.example_used,
                "final_response": trial.final_response,
                "completed": trial.completed,
                "safety_flag": trial.safety_flag,
            }
        )

    return templates.TemplateResponse(
        request,
        "admin_participant_detail.html",
        {"participant": participant, "trial_rows": trial_rows},
    )


@router.get("/items")
def admin_items(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    items = db.query(Item).order_by(Item.item_id).all()
    return templates.TemplateResponse(request, "admin_items.html", {"items": items, "result": None})


@router.post("/items/upload")
async def admin_items_upload(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    content = await file.read()
    try:
        rows = parse_item_file(file.filename, content)
        upserted, errors = upsert_items(db, rows)
        result = {"upserted": upserted, "errors": errors}
    except Exception as exc:
        result = {"upserted": 0, "errors": [f"파일을 읽는 중 오류가 발생했습니다: {exc}"]}

    items = db.query(Item).order_by(Item.item_id).all()
    return templates.TemplateResponse(request, "admin_items.html", {"items": items, "result": result})
