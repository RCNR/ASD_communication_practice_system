from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.item import Item
from app.models.participant import Participant
from app.models.trial_response import TrialResponse
from app.services.session_service import (
    advance_phase_if_needed,
    get_current_trial,
    get_or_create_active_session,
    mark_session_completed,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _current_participant(request: Request, db: Session) -> Participant | None:
    participant_code = request.session.get("participant_code")
    if not participant_code:
        return None
    return db.query(Participant).filter_by(participant_code=participant_code).first()


@router.get("/session")
def session_screen(request: Request, db: Session = Depends(get_db)):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    study_session = get_or_create_active_session(db, participant)
    if study_session is None:
        return templates.TemplateResponse(request, "study_complete.html")

    trial = get_current_trial(db, study_session)

    if trial is None:
        mark_session_completed(db, study_session)
        advance_phase_if_needed(db, participant)
        return templates.TemplateResponse(request, "session_complete.html")

    if trial.first_response_started_at is None:
        trial.first_response_started_at = datetime.now(timezone.utc)
        db.commit()

    item = db.get(Item, trial.item_id)

    return templates.TemplateResponse(
        request,
        "session_item.html",
        {
            "item": item,
            "trial_id": trial.id,
            "progress_current": trial.item_order,
            "progress_total": study_session.planned_item_count,
        },
    )


@router.post("/session/respond")
def session_respond(
    request: Request,
    trial_id: int = Form(...),
    response_text: str = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed:
        trial.first_response = response_text
        trial.first_response_submitted_at = datetime.now(timezone.utc)
        trial.final_response = response_text
        trial.completed = True
        db.commit()

    return RedirectResponse(url="/session", status_code=303)
