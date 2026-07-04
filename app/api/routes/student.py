from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.item import Item
from app.models.participant import Participant
from app.models.phase_config import PhaseConfig
from app.models.trial_response import TrialResponse
from app.services.hint_service import request_hint
from app.services.safety_service import detect_safety_flag
from app.services.session_service import (
    advance_phase_if_needed,
    get_current_trial,
    get_latest_hint_message,
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


def _render_intervention_item(request: Request, db: Session, trial: TrialResponse, planned_item_count: int):
    item = db.get(Item, trial.item_id)

    if trial.first_response is None:
        return templates.TemplateResponse(
            request,
            "intervention_item.html",
            {
                "item": item,
                "trial_id": trial.id,
                "progress_current": trial.item_order,
                "progress_total": planned_item_count,
                "stage": "first",
            },
        )

    hint1 = get_latest_hint_message(db, trial.id, 1)
    hint2 = get_latest_hint_message(db, trial.id, 2)

    hints_shown = []
    if hint1:
        hints_shown.append({"level": 1, "message": hint1})
    if hint2:
        hints_shown.append({"level": 2, "message": hint2})

    needs_revise_level = None
    if hint1 and trial.revised_response_1 is None:
        needs_revise_level = 1
    elif hint2 and trial.revised_response_2 is None:
        needs_revise_level = 2

    can_request_hint1 = hint1 is None
    can_request_hint2 = hint1 is not None and trial.revised_response_1 is not None and hint2 is None

    return templates.TemplateResponse(
        request,
        "intervention_item.html",
        {
            "item": item,
            "trial_id": trial.id,
            "progress_current": trial.item_order,
            "progress_total": planned_item_count,
            "stage": "hint_flow",
            "first_response": trial.first_response,
            "hints_shown": hints_shown,
            "needs_revise_level": needs_revise_level,
            "can_request_hint1": can_request_hint1,
            "can_request_hint2": can_request_hint2,
            "example_used": trial.example_used,
            "example_text": item.verified_example if trial.example_used else None,
        },
    )


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

    if trial.safety_flag:
        return templates.TemplateResponse(request, "safety_warning.html", {"trial_id": trial.id})

    if trial.first_response_started_at is None and study_session.phase != "intervention":
        trial.first_response_started_at = datetime.now(timezone.utc)
        db.commit()

    if study_session.phase == "intervention":
        return _render_intervention_item(request, db, trial, study_session.planned_item_count)

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
        flag = detect_safety_flag(response_text)
        if flag:
            trial.safety_flag = flag
        else:
            trial.final_response = response_text
            trial.completed = True
        db.commit()

    return RedirectResponse(url="/session", status_code=303)


@router.post("/session/first-response")
def session_first_response(
    request: Request,
    trial_id: int = Form(...),
    response_text: str = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed and trial.first_response is None:
        trial.first_response = response_text
        trial.first_response_started_at = trial.first_response_started_at or datetime.now(timezone.utc)
        trial.first_response_submitted_at = datetime.now(timezone.utc)
        flag = detect_safety_flag(response_text)
        if flag:
            trial.safety_flag = flag
        db.commit()

    return RedirectResponse(url="/session", status_code=303)


@router.post("/session/hint")
def session_hint(
    request: Request,
    trial_id: int = Form(...),
    hint_level: int = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed:
        phase_config = db.get(PhaseConfig, trial.phase)
        if phase_config and phase_config.ai_hint_enabled:
            item = db.get(Item, trial.item_id)
            request_hint(db, trial, item, hint_level)

    return RedirectResponse(url="/session", status_code=303)


@router.post("/session/revise")
def session_revise(
    request: Request,
    trial_id: int = Form(...),
    hint_level: int = Form(...),
    response_text: str = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed:
        if hint_level == 1:
            trial.revised_response_1 = response_text
        elif hint_level == 2:
            trial.revised_response_2 = response_text
        flag = detect_safety_flag(response_text)
        if flag:
            trial.safety_flag = flag
        db.commit()

    return RedirectResponse(url="/session", status_code=303)


@router.post("/session/example")
def session_example(
    request: Request,
    trial_id: int = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed:
        trial.example_used = True
        db.commit()

    return RedirectResponse(url="/session", status_code=303)


@router.post("/session/finalize")
def session_finalize(
    request: Request,
    trial_id: int = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed and trial.first_response is not None:
        trial.final_response = trial.revised_response_2 or trial.revised_response_1 or trial.first_response
        trial.completed = True
        db.commit()

    return RedirectResponse(url="/session", status_code=303)


@router.post("/session/safety-acknowledge")
def session_safety_acknowledge(
    request: Request,
    trial_id: int = Form(...),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    trial = db.get(TrialResponse, trial_id)
    if trial and not trial.completed and trial.safety_flag:
        trial.final_response = trial.revised_response_2 or trial.revised_response_1 or trial.first_response
        trial.completed = True
        db.commit()

    return RedirectResponse(url="/session", status_code=303)
