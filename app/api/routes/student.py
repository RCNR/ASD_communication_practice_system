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
from app.services.hint_service import evaluate_answer
from app.services.safety_service import detect_safety_flag
from app.services.session_service import (
    advance_phase_if_needed,
    check_wait_gate,
    completed_session_count,
    get_current_trial,
    get_latest_evaluation,
    get_or_create_active_session,
    get_target_session_count,
    mark_session_completed,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PHASE_LABEL = {"baseline": "기초선", "intervention": "중재", "maintenance": "유지"}


def _current_participant(request: Request, db: Session) -> Participant | None:
    participant_code = request.session.get("participant_code")
    if not participant_code:
        return None
    return db.query(Participant).filter_by(participant_code=participant_code).first()


def _render_intervention_item(
    request: Request,
    db: Session,
    trial: TrialResponse,
    planned_item_count: int,
    session_label: dict,
):
    item = db.get(Item, trial.item_id)
    base_context = {
        "item": item,
        "trial_id": trial.id,
        "progress_current": trial.item_order,
        "progress_total": planned_item_count,
        **session_label,
    }

    if trial.first_response is None:
        return templates.TemplateResponse(
            request, "intervention_item.html", {**base_context, "stage": "first"}
        )

    eval1 = get_latest_evaluation(db, trial.id, 1)

    if eval1 is None or eval1.score_level == 2:
        return templates.TemplateResponse(
            request,
            "intervention_item.html",
            {**base_context, "stage": "adequate", "first_response": trial.first_response},
        )

    if trial.revised_response_1 is None:
        return templates.TemplateResponse(
            request,
            "intervention_item.html",
            {
                **base_context,
                "stage": "hint_wait_revision",
                "first_response": trial.first_response,
                "feedback_message": eval1.hint_message,
            },
        )

    eval2 = get_latest_evaluation(db, trial.id, 2)

    if eval2 is None or eval2.score_level == 2:
        return templates.TemplateResponse(
            request,
            "intervention_item.html",
            {**base_context, "stage": "adequate", "first_response": trial.first_response},
        )

    return templates.TemplateResponse(
        request,
        "intervention_item.html",
        {
            **base_context,
            "stage": "example_wait_final_revision",
            "first_response": trial.first_response,
            "example_text": item.verified_example,
        },
    )


@router.get("/session")
def session_screen(request: Request, db: Session = Depends(get_db)):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/login", status_code=303)

    available_at = check_wait_gate(db, participant)
    if available_at is not None:
        return templates.TemplateResponse(
            request, "waiting.html", {"available_at_iso": available_at.isoformat()}
        )

    study_session = get_or_create_active_session(db, participant)
    if study_session is None:
        return templates.TemplateResponse(request, "study_complete.html")

    trial = get_current_trial(db, study_session)

    if trial is None:
        mark_session_completed(db, study_session)
        advance_phase_if_needed(db, participant)

        study_finished = participant.current_phase == "maintenance" and completed_session_count(
            db, participant
        ) >= get_target_session_count(participant)

        context = {"study_finished": study_finished}
        if not study_finished:
            next_available_at = check_wait_gate(db, participant)
            if next_available_at is not None:
                context["available_at_iso"] = next_available_at.isoformat()
            else:
                context["can_continue"] = True

        return templates.TemplateResponse(request, "session_complete.html", context)

    if trial.safety_flag:
        return templates.TemplateResponse(request, "safety_warning.html", {"trial_id": trial.id})

    if trial.first_response_started_at is None and study_session.phase != "intervention":
        trial.first_response_started_at = datetime.now(timezone.utc)
        db.commit()

    session_label = {
        "phase_label": PHASE_LABEL[study_session.phase],
        "session_number": study_session.session_number,
        "session_target": get_target_session_count(participant),
    }

    if study_session.phase == "intervention":
        return _render_intervention_item(
            request, db, trial, study_session.planned_item_count, session_label
        )

    item = db.get(Item, trial.item_id)
    return templates.TemplateResponse(
        request,
        "session_item.html",
        {
            "item": item,
            "trial_id": trial.id,
            "progress_current": trial.item_order,
            "progress_total": study_session.planned_item_count,
            **session_label,
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

        if not flag:
            phase_config = db.get(PhaseConfig, trial.phase)
            if phase_config and phase_config.ai_hint_enabled:
                item = db.get(Item, trial.item_id)
                evaluate_answer(db, trial, item, hint_level=1, student_response=response_text)

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
    if not trial or trial.completed:
        return RedirectResponse(url="/session", status_code=303)

    flag = detect_safety_flag(response_text)

    if hint_level == 1:
        # revision after the step-2 hint: save it, then let the AI check it again
        trial.revised_response_1 = response_text
        if flag:
            trial.safety_flag = flag
        db.commit()

        if not flag:
            phase_config = db.get(PhaseConfig, trial.phase)
            if phase_config and phase_config.ai_hint_enabled:
                item = db.get(Item, trial.item_id)
                score, _ = evaluate_answer(
                    db, trial, item, hint_level=2, student_response=response_text
                )
                if score != 2:
                    trial.example_used = True
                    db.commit()

    elif hint_level == 2:
        # final revision after seeing the verified example: save and finalize, no further AI check
        trial.revised_response_2 = response_text
        if flag:
            trial.safety_flag = flag
        else:
            trial.final_response = response_text
            trial.completed = True
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
