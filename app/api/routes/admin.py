from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.item import Item
from app.models.participant import Participant
from app.models.phase_config import PhaseConfig
from app.models.session import StudySession
from app.models.session_item import SessionItem
from app.models.trial_response import TrialResponse
from app.services.item_import_service import parse_item_file, upsert_items
from app.services.session_service import PHASE_ORDER, get_latest_evaluation, get_latest_hint_message

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _measured_score(trial: TrialResponse, eval_log) -> int | None:
    """The score used for research measurement/display. Forced to 0 if the
    participant's true independent first attempt (first_attempt_response)
    was rejected by the validity/safety/profanity gate before an acceptable
    answer was reached (i.e. it differs from what actually got saved into
    first_response) - regardless of what that accepted retry's own AI
    judgment (eval_log.score_level) came out to.

    This is a display-only override: it never touches AiHintLog.score_level
    itself, which still drives the intervention hint/pass flow honestly (see
    student.py's session_first_response) - only what's shown/summed here."""
    if eval_log is None:
        return None
    if (
        trial.first_attempt_response is not None
        and trial.first_response is not None
        and trial.first_attempt_response != trial.first_response
    ):
        return 0
    return eval_log.score_level


def _require_admin(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url="/home", status_code=303)
    return None


@router.get("/logout")
def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse(url="/home", status_code=303)


@router.get("")
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    phase_length_field = {
        "baseline": "baseline_length",
        "intervention": "intervention_length",
        "maintenance": "maintenance_length",
    }

    rows = []
    for participant in db.query(Participant).order_by(Participant.participant_code).all():
        session_counts = {
            phase: {
                "completed": db.query(StudySession)
                .filter_by(participant_code=participant.participant_code, phase=phase, status="completed")
                .count(),
                "target": getattr(participant, phase_length_field[phase]),
            }
            for phase in PHASE_ORDER
        }
        rows.append(
            {
                "participant": participant,
                "session_counts": session_counts,
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
    intervention_length: int = Form(20),
    maintenance_length: int = Form(2),
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
            intervention_length=intervention_length,
            maintenance_length=maintenance_length,
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
        request,
        "admin_participant_edit.html",
        {"participant": participant, "error": None},
    )


@router.post("/participants/{participant_code}/edit")
def admin_participant_edit_submit(
    request: Request,
    participant_code: str,
    baseline_length: int = Form(...),
    intervention_length: int = Form(...),
    maintenance_length: int = Form(...),
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
    participant.intervention_length = intervention_length
    participant.maintenance_length = maintenance_length
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
        eval1 = get_latest_evaluation(db, trial.id, 1)
        eval2 = get_latest_evaluation(db, trial.id, 2)
        trial_rows.append(
            {
                "phase": study_session.phase,
                "session_number": study_session.session_number,
                "item_order": trial.item_order,
                "item_text": item.item_text,
                "first_attempt_response": trial.first_attempt_response,
                "first_response": trial.first_response,
                "score1": _measured_score(trial, eval1),
                "hint1": get_latest_hint_message(db, trial.id, 1),
                "revised_response_1": trial.revised_response_1,
                "score2": eval2.score_level if eval2 else None,
                "hint2": get_latest_hint_message(db, trial.id, 2),
                "revised_response_2": trial.revised_response_2,
                "example_used": trial.example_used,
                "final_response": trial.final_response,
                "completed": trial.completed,
            }
        )

    return templates.TemplateResponse(
        request,
        "admin_participant_detail.html",
        {"participant": participant, "trial_rows": trial_rows},
    )


@router.get("/scores")
def admin_scores(request: Request, participant_code: str = "", db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participant_codes = [p.participant_code for p in db.query(Participant).order_by(Participant.participant_code)]

    query = (
        db.query(TrialResponse, StudySession, Item, Participant)
        .join(StudySession, TrialResponse.session_id == StudySession.id)
        .join(Item, TrialResponse.item_id == Item.item_id)
        .join(Participant, StudySession.participant_code == Participant.participant_code)
        .filter(StudySession.phase == "intervention")
    )
    if participant_code:
        query = query.filter(Participant.participant_code == participant_code)

    trials = query.order_by(
        Participant.participant_code, StudySession.session_number, TrialResponse.item_order
    ).all()

    score_rows = []
    session_totals = {}  # (participant_code, session_number) -> {"total": int, "count": int}
    for trial, study_session, item, participant in trials:
        eval1 = get_latest_evaluation(db, trial.id, 1)
        eval2 = get_latest_evaluation(db, trial.id, 2)
        score1 = _measured_score(trial, eval1)
        score_rows.append(
            {
                "participant_code": participant.participant_code,
                "session_number": study_session.session_number,
                "item_order": trial.item_order,
                "item_text": item.item_text,
                "score1": score1,
                "score2": eval2.score_level if eval2 else None,
                "example_used": trial.example_used,
                "completed": trial.completed,
            }
        )

        key = (participant.participant_code, study_session.session_number)
        totals = session_totals.setdefault(key, {"total": 0, "count": 0, "answered": 0})
        totals["count"] += 1
        if score1 is not None:
            totals["total"] += score1
            totals["answered"] += 1

    session_summary_rows = [
        {
            "participant_code": code,
            "session_number": session_number,
            "score_1st_total": totals["total"],
            "item_count": totals["count"],
            "answered_count": totals["answered"],
        }
        for (code, session_number), totals in sorted(session_totals.items())
    ]

    return templates.TemplateResponse(
        request,
        "admin_scores.html",
        {
            "score_rows": score_rows,
            "session_summary_rows": session_summary_rows,
            "participant_codes": participant_codes,
            "selected_participant_code": participant_code,
        },
    )


@router.get("/items")
def admin_items(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    items = db.query(Item).filter(Item.use_type != "pretraining").order_by(Item.item_id).all()
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

    items = db.query(Item).filter(Item.use_type != "pretraining").order_by(Item.item_id).all()
    return templates.TemplateResponse(request, "admin_items.html", {"items": items, "result": result})


@router.post("/items/delete-all")
def admin_items_delete_all(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    used_item_ids = {item_id for (item_id,) in db.query(SessionItem.item_id).distinct().all()}

    query = db.query(Item).filter(Item.use_type != "pretraining")
    if used_item_ids:
        query = query.filter(Item.item_id.notin_(used_item_ids))
    to_delete = query.all()
    deleted_count = len(to_delete)
    for item in to_delete:
        db.delete(item)
    db.commit()

    message = f"{deleted_count}개 문항을 삭제했습니다."
    if used_item_ids:
        message += f" (이미 회기에 배정된 {len(used_item_ids)}개 문항은 삭제하지 않았습니다.)"

    items = db.query(Item).filter(Item.use_type != "pretraining").order_by(Item.item_id).all()
    return templates.TemplateResponse(
        request, "admin_items.html", {"items": items, "result": None, "delete_message": message}
    )


@router.get("/pretraining-items")
def admin_pretraining_items(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    items = db.query(Item).filter_by(use_type="pretraining").order_by(Item.item_id).all()
    return templates.TemplateResponse(request, "admin_pretraining_items.html", {"items": items, "result": None})


@router.post("/pretraining-items/upload")
async def admin_pretraining_items_upload(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    content = await file.read()
    try:
        rows = parse_item_file(file.filename, content)
        for row in rows:
            row["use_type"] = "pretraining"
            # item_id is the Item table's primary key, shared with regular
            # items - prefixing here guarantees a pretraining upload can
            # never collide with (and silently overwrite via db.merge) a
            # regular item that happens to use the same item_id in its own
            # source file.
            original_id = str(row.get("item_id") or "").strip()
            if original_id and not original_id.startswith("PRETRAIN_"):
                row["item_id"] = f"PRETRAIN_{original_id}"
        upserted, errors = upsert_items(db, rows)
        result = {"upserted": upserted, "errors": errors}
    except Exception as exc:
        result = {"upserted": 0, "errors": [f"파일을 읽는 중 오류가 발생했습니다: {exc}"]}

    items = db.query(Item).filter_by(use_type="pretraining").order_by(Item.item_id).all()
    return templates.TemplateResponse(request, "admin_pretraining_items.html", {"items": items, "result": result})


@router.post("/pretraining-items/delete-all")
def admin_pretraining_items_delete_all(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    to_delete = db.query(Item).filter_by(use_type="pretraining").all()
    deleted_count = len(to_delete)
    for item in to_delete:
        db.delete(item)
    db.commit()

    items = db.query(Item).filter_by(use_type="pretraining").order_by(Item.item_id).all()
    return templates.TemplateResponse(
        request,
        "admin_pretraining_items.html",
        {"items": items, "result": None, "delete_message": f"{deleted_count}개 문항을 삭제했습니다."},
    )


@router.get("/phase-config")
def admin_phase_config_form(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    configs = {c.phase: c for c in db.query(PhaseConfig).all()}
    return templates.TemplateResponse(request, "admin_phase_config.html", {"configs": configs})


@router.post("/phase-config")
def admin_phase_config_submit(
    request: Request,
    baseline_item_count: int = Form(...),
    intervention_item_count: int = Form(...),
    maintenance_item_count: int = Form(...),
    db: Session = Depends(get_db),
):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    item_counts = {
        "baseline": baseline_item_count,
        "intervention": intervention_item_count,
        "maintenance": maintenance_item_count,
    }

    for phase in PHASE_ORDER:
        config = db.get(PhaseConfig, phase)
        if config is None:
            config = PhaseConfig(phase=phase)
            db.add(config)
        config.default_item_count = item_counts[phase]

    db.commit()
    return RedirectResponse(url="/admin", status_code=303)
