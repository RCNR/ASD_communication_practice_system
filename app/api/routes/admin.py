from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.participant import Participant
from app.models.phase_config import PhaseConfig
from app.models.session import StudySession
from app.models.session_fidelity_check import SessionFidelityCheck
from app.models.session_item import SessionItem
from app.models.trial_response import TrialResponse
from app.services.item_import_service import parse_item_file, upsert_items
from app.services.session_service import PHASE_ORDER, get_latest_evaluation, get_latest_hint_message

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


def _require_admin(request: Request):
    if not request.session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def _combine_seconds(days: int, hours: int, minutes: int, seconds: int) -> int:
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _split_seconds(total: int) -> dict:
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return {"days": days, "hours": hours, "minutes": minutes, "seconds": seconds}


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
    intervention_length: int = Form(20),
    maintenance_length: int = Form(2),
    baseline_wait_d: int = Form(0),
    baseline_wait_h: int = Form(0),
    baseline_wait_m: int = Form(0),
    baseline_wait_s: int = Form(0),
    intervention_wait_d: int = Form(0),
    intervention_wait_h: int = Form(0),
    intervention_wait_m: int = Form(0),
    intervention_wait_s: int = Form(0),
    maintenance_wait_d: int = Form(0),
    maintenance_wait_h: int = Form(0),
    maintenance_wait_m: int = Form(0),
    maintenance_wait_s: int = Form(0),
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
            baseline_wait_seconds=_combine_seconds(baseline_wait_d, baseline_wait_h, baseline_wait_m, baseline_wait_s),
            intervention_wait_seconds=_combine_seconds(
                intervention_wait_d, intervention_wait_h, intervention_wait_m, intervention_wait_s
            ),
            maintenance_wait_seconds=_combine_seconds(
                maintenance_wait_d, maintenance_wait_h, maintenance_wait_m, maintenance_wait_s
            ),
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

    wait = {
        "baseline": _split_seconds(participant.baseline_wait_seconds),
        "intervention": _split_seconds(participant.intervention_wait_seconds),
        "maintenance": _split_seconds(participant.maintenance_wait_seconds),
    }

    return templates.TemplateResponse(
        request,
        "admin_participant_edit.html",
        {"participant": participant, "wait": wait, "error": None},
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
    baseline_wait_d: int = Form(0),
    baseline_wait_h: int = Form(0),
    baseline_wait_m: int = Form(0),
    baseline_wait_s: int = Form(0),
    intervention_wait_d: int = Form(0),
    intervention_wait_h: int = Form(0),
    intervention_wait_m: int = Form(0),
    intervention_wait_s: int = Form(0),
    maintenance_wait_d: int = Form(0),
    maintenance_wait_h: int = Form(0),
    maintenance_wait_m: int = Form(0),
    maintenance_wait_s: int = Form(0),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participant = db.query(Participant).filter_by(participant_code=participant_code).first()
    if not participant:
        return RedirectResponse(url="/admin", status_code=303)

    wait = {
        "baseline": _split_seconds(participant.baseline_wait_seconds),
        "intervention": _split_seconds(participant.intervention_wait_seconds),
        "maintenance": _split_seconds(participant.maintenance_wait_seconds),
    }

    if current_phase not in PHASE_ORDER:
        return templates.TemplateResponse(
            request,
            "admin_participant_edit.html",
            {"participant": participant, "wait": wait, "error": "올바르지 않은 단계입니다."},
        )
    if status not in ("active", "paused", "dropped"):
        return templates.TemplateResponse(
            request,
            "admin_participant_edit.html",
            {"participant": participant, "wait": wait, "error": "올바르지 않은 상태입니다."},
        )

    participant.baseline_length = baseline_length
    participant.intervention_length = intervention_length
    participant.maintenance_length = maintenance_length
    participant.current_phase = current_phase
    participant.status = status
    participant.baseline_wait_seconds = _combine_seconds(baseline_wait_d, baseline_wait_h, baseline_wait_m, baseline_wait_s)
    participant.intervention_wait_seconds = _combine_seconds(
        intervention_wait_d, intervention_wait_h, intervention_wait_m, intervention_wait_s
    )
    participant.maintenance_wait_seconds = _combine_seconds(
        maintenance_wait_d, maintenance_wait_h, maintenance_wait_m, maintenance_wait_s
    )
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
                "first_response": trial.first_response,
                "score1": eval1.score_level if eval1 else None,
                "hint1": get_latest_hint_message(db, trial.id, 1),
                "revised_response_1": trial.revised_response_1,
                "score2": eval2.score_level if eval2 else None,
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
        score1 = eval1.score_level if eval1 else None
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


FIDELITY_FIELDS = [
    "hint_matches_item",
    "no_new_situation",
    "no_scoring",
    "no_points_given",
    "no_full_answer_given",
    "no_personal_info_request",
    "no_risky_advice",
    "hint_level_respected",
]


@router.get("/fidelity")
def admin_fidelity_list(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    sessions = (
        db.query(StudySession)
        .filter_by(phase="intervention", status="completed")
        .order_by(StudySession.participant_code, StudySession.session_number)
        .all()
    )

    rows = []
    for study_session in sessions:
        hint_count = (
            db.query(AiHintLog)
            .join(TrialResponse, AiHintLog.trial_id == TrialResponse.id)
            .filter(TrialResponse.session_id == study_session.id)
            .count()
        )
        check = db.get(SessionFidelityCheck, study_session.id)
        rows.append(
            {
                "session": study_session,
                "hint_count": hint_count,
                "reviewed": check is not None and check.reviewed_at is not None,
            }
        )

    return templates.TemplateResponse(request, "admin_fidelity_list.html", {"rows": rows})


@router.get("/fidelity/{session_id}")
def admin_fidelity_detail(request: Request, session_id: int, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    study_session = db.get(StudySession, session_id)
    if not study_session:
        return RedirectResponse(url="/admin/fidelity", status_code=303)

    hint_logs = (
        db.query(AiHintLog)
        .join(TrialResponse, AiHintLog.trial_id == TrialResponse.id)
        .filter(TrialResponse.session_id == session_id)
        .order_by(AiHintLog.created_at)
        .all()
    )

    summary = {
        "total": len(hint_logs),
        "any_scoring": any(log.contains_scoring for log in hint_logs),
        "any_full_answer": any(log.contains_full_answer for log in hint_logs),
        "any_unsafe": any(log.safety_flag != "none" for log in hint_logs),
        "any_fallback": any(log.fallback_used for log in hint_logs),
    }

    check = db.get(SessionFidelityCheck, session_id)

    return templates.TemplateResponse(
        request,
        "admin_fidelity_detail.html",
        {
            "study_session": study_session,
            "hint_logs": hint_logs,
            "summary": summary,
            "check": check,
        },
    )


@router.post("/fidelity/{session_id}")
async def admin_fidelity_submit(request: Request, session_id: int, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    study_session = db.get(StudySession, session_id)
    if not study_session:
        return RedirectResponse(url="/admin/fidelity", status_code=303)

    form = await request.form()

    check = db.get(SessionFidelityCheck, session_id)
    if check is None:
        check = SessionFidelityCheck(session_id=session_id)
        db.add(check)

    for field in FIDELITY_FIELDS:
        setattr(check, field, field in form)
    check.note = form.get("note") or None
    check.reviewed_at = datetime.now(timezone.utc)

    db.commit()
    return RedirectResponse(url=f"/admin/fidelity/{session_id}", status_code=303)


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
    intervention_ai_hint_enabled: bool = Form(False),
    maintenance_ai_hint_enabled: bool = Form(False),
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
    ai_hint_enabled = {
        "baseline": False,  # brief: 기초선 단계는 AI 사용 안 함 (절대 켜지 않음)
        "intervention": intervention_ai_hint_enabled,
        "maintenance": maintenance_ai_hint_enabled,
    }

    for phase in PHASE_ORDER:
        config = db.get(PhaseConfig, phase)
        if config is None:
            config = PhaseConfig(phase=phase)
            db.add(config)
        config.default_item_count = item_counts[phase]
        config.ai_hint_enabled = ai_hint_enabled[phase]

    db.commit()
    return RedirectResponse(url="/admin", status_code=303)
