from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.participant import Participant
from app.models.session import StudySession
from app.models.session_item import SessionItem
from app.models.trial_response import TrialResponse
from app.services.item_import_service import (
    PRACTICE_USE_TYPES,
    SESSION_USE_TYPES,
    parse_item_file,
    upsert_items,
)
from app.services.session_service import (
    INTERVENTION_EXIT_STREAK,
    INTERVENTION_MIN_SESSIONS,
    PHASE_ORDER,
    STABILITY_EXIT_STREAK,
    get_latest_evaluation,
    get_latest_hint_message,
    is_monotonic,
    measured_score,
    meets_mastery,
    phase_ended_early,
    session_score,
    trend_label,
)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


# 관리자 화면은 참여자 화면(student.py의 PHASE_LABEL)과 달리 연구 용어를 쓴다.
PHASE_LABEL = {"baseline": "기초선", "intervention": "중재", "maintenance": "유지"}

PHASE_LENGTH_FIELD = {
    "baseline": "baseline_length",
    "intervention": "intervention_length",
    "maintenance": "maintenance_length",
}


def _phase_score_block(db: Session, participant: Participant, phase: str) -> dict:
    """한 참여자의 한 단계를 회기별 점수 + 전이 판정으로 정리한다.

    판정은 실제 전이에 쓰이는 것과 같은 함수(meets_mastery / is_monotonic)를
    각 회기 시점의 직전 STREAK개 회기에 다시 적용해서 재현한다. 두 번 구현하지
    않으므로 표에 찍힌 판정과 참여자가 실제로 겪은 전이가 어긋날 수 없다."""
    is_intervention = phase == "intervention"
    streak = INTERVENTION_EXIT_STREAK if is_intervention else STABILITY_EXIT_STREAK
    # 판정을 시작하는 회기. 중재는 최소 회기를 채우기 전에는 기준을 충족해도
    # 전이하지 않으므로 표에도 표시하지 않는다.
    first_judged = max(streak, INTERVENTION_MIN_SESSIONS) if is_intervention else streak

    sessions = (
        db.query(StudySession)
        .filter_by(participant_code=participant.participant_code, phase=phase, status="completed")
        .order_by(StudySession.session_number)
        .all()
    )
    scores = [session_score(db, s) for s in sessions]

    rows = []
    met_at = None
    for i, (study_session, (earned, possible)) in enumerate(zip(sessions, scores)):
        judgment, met = "", False
        if i + 1 >= first_judged and met_at is None:
            window = scores[i + 1 - streak : i + 1]
            earned_window = [e for e, _ in window]
            if is_intervention:
                met = meets_mastery(window)
                judgment = "연속 {}회기 75% 이상".format(streak) if met else ""
            else:
                met = is_monotonic(earned_window)
                judgment = (
                    "{} {}".format(" → ".join(str(e) for e in earned_window), trend_label(earned_window))
                    if met
                    else ""
                )
            if met:
                met_at = study_session.session_number
        rows.append(
            {
                "session_number": study_session.session_number,
                "earned": earned,
                "possible": possible,
                "percent": round(earned * 100 / possible) if possible else None,
                "judgment": judgment,
                "met": met,
            }
        )

    target = getattr(participant, PHASE_LENGTH_FIELD[phase])
    ended_early = phase_ended_early(db, participant, phase)
    if ended_early:
        outcome = "조기 종료 (기준 충족)"
    elif len(sessions) >= target:
        outcome = "계획 회기 소진"
    else:
        outcome = "진행 중"

    return {
        "participant_code": participant.participant_code,
        "phase": phase,
        "phase_label": PHASE_LABEL[phase],
        "target": target,
        "sessions": rows,
        "skipped": list(range(len(sessions) + 1, target + 1)) if ended_early else [],
        "outcome": outcome,
        "ended_early": ended_early,
    }


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

    rows = []
    for participant in db.query(Participant).order_by(Participant.participant_code).all():
        session_counts = {
            phase: {
                "completed": db.query(StudySession)
                .filter_by(participant_code=participant.participant_code, phase=phase, status="completed")
                .count(),
                "target": getattr(participant, PHASE_LENGTH_FIELD[phase]),
                # 조기 종료된 단계는 완료 수가 목표보다 작다 - 표시를 구분하지
                # 않으면 "아직 남았다"로 오해된다.
                "ended_early": phase_ended_early(db, participant, phase),
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
    maintenance_length: int = Form(4),
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


@router.post("/participants/{participant_code}/delete")
def admin_participant_delete(request: Request, participant_code: str, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participant = db.query(Participant).filter_by(participant_code=participant_code).first()
    if not participant:
        return RedirectResponse(url="/admin", status_code=303)

    session_ids = [
        row[0]
        for row in db.query(StudySession.id).filter_by(participant_code=participant_code).all()
    ]
    trial_ids = [
        row[0]
        for row in db.query(TrialResponse.id).filter(TrialResponse.session_id.in_(session_ids)).all()
    ]
    db.query(AiHintLog).filter(AiHintLog.trial_id.in_(trial_ids)).delete(synchronize_session=False)
    db.query(TrialResponse).filter(TrialResponse.session_id.in_(session_ids)).delete(synchronize_session=False)
    db.query(StudySession).filter_by(participant_code=participant_code).delete(synchronize_session=False)
    db.delete(participant)
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
                # 무효 회기는 같은 회기 번호의 새 회기와 구분되도록 표시만 남긴다.
                "phase": study_session.phase + (" (무효)" if study_session.status == "stopped" else ""),
                "session_number": study_session.session_number,
                "item_order": trial.item_order,
                "item_text": item.item_text,
                "first_attempt_response": trial.first_attempt_response,
                "first_response": trial.first_response,
                "score1": measured_score(trial, eval1),
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
def admin_scores(
    request: Request,
    participant_code: str = "",
    phase: str = "",
    db: Session = Depends(get_db),
):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    participants = db.query(Participant).order_by(Participant.participant_code).all()
    participant_codes = [p.participant_code for p in participants]

    selected_phases = [phase] if phase in PHASE_ORDER else PHASE_ORDER
    selected_participants = [
        p for p in participants if not participant_code or p.participant_code == participant_code
    ]

    phase_blocks = [
        _phase_score_block(db, p, ph) for p in selected_participants for ph in selected_phases
    ]
    phase_blocks = [b for b in phase_blocks if b["sessions"] or b["skipped"]]

    query = (
        db.query(TrialResponse, StudySession, Item, Participant)
        .join(StudySession, TrialResponse.session_id == StudySession.id)
        .join(Item, TrialResponse.item_id == Item.item_id)
        .join(Participant, StudySession.participant_code == Participant.participant_code)
        .filter(StudySession.phase.in_(selected_phases))
        .filter(StudySession.status != "stopped")
    )
    if participant_code:
        query = query.filter(Participant.participant_code == participant_code)

    trials = query.order_by(
        Participant.participant_code,
        StudySession.phase,
        StudySession.session_number,
        TrialResponse.item_order,
    ).all()

    score_rows = [
        {
            "participant_code": participant.participant_code,
            "phase_label": PHASE_LABEL[study_session.phase],
            "session_number": study_session.session_number,
            "item_order": trial.item_order,
            "item_text": item.item_text,
            "score1": measured_score(trial, get_latest_evaluation(db, trial.id, 1)),
            "score2": (
                get_latest_evaluation(db, trial.id, 2).score_level
                if get_latest_evaluation(db, trial.id, 2)
                else None
            ),
            "example_used": trial.example_used,
            "completed": trial.completed,
        }
        for trial, study_session, item, participant in trials
    ]

    return templates.TemplateResponse(
        request,
        "admin_scores.html",
        {
            "score_rows": score_rows,
            "phase_blocks": phase_blocks,
            "participant_codes": participant_codes,
            "selected_participant_code": participant_code,
            "selected_phase": phase if phase in PHASE_ORDER else "",
            "phase_labels": PHASE_LABEL,
            "phase_order": PHASE_ORDER,
        },
    )
def _items_of(db: Session, use_types: tuple[str, ...]) -> list[Item]:
    """Item bank rows for one upload screen, listed the way they'll be shown to
    participants (set by set, in-set order) rather than by item_id."""
    return (
        db.query(Item)
        .filter(Item.use_type.in_(use_types))
        .order_by(Item.use_type, Item.set_no, Item.set_order)
        .all()
    )


@router.get("/items")
def admin_items(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request, "admin_items.html", {"items": _items_of(db, SESSION_USE_TYPES), "result": None}
    )


@router.post("/items/upload")
async def admin_items_upload(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    content = await file.read()
    try:
        rows = parse_item_file(file.filename, content)
        upserted, errors = upsert_items(db, rows, SESSION_USE_TYPES)
        result = {"upserted": upserted, "errors": errors}
    except Exception as exc:
        result = {"upserted": 0, "errors": [f"파일을 읽는 중 오류가 발생했습니다: {exc}"]}

    return templates.TemplateResponse(
        request, "admin_items.html", {"items": _items_of(db, SESSION_USE_TYPES), "result": result}
    )


@router.post("/items/delete-all")
def admin_items_delete_all(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    used_item_ids = {item_id for (item_id,) in db.query(SessionItem.item_id).distinct().all()}

    query = db.query(Item).filter(Item.use_type.in_(SESSION_USE_TYPES))
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

    return templates.TemplateResponse(
        request,
        "admin_items.html",
        {"items": _items_of(db, SESSION_USE_TYPES), "result": None, "delete_message": message},
    )


@router.get("/pretraining-items")
def admin_pretraining_items(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request,
        "admin_pretraining_items.html",
        {"items": _items_of(db, PRACTICE_USE_TYPES), "result": None},
    )


@router.post("/pretraining-items/upload")
async def admin_pretraining_items_upload(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    content = await file.read()
    try:
        rows = parse_item_file(file.filename, content)
        upserted, errors = upsert_items(db, rows, PRACTICE_USE_TYPES)
        result = {"upserted": upserted, "errors": errors}
    except Exception as exc:
        result = {"upserted": 0, "errors": [f"파일을 읽는 중 오류가 발생했습니다: {exc}"]}

    return templates.TemplateResponse(
        request,
        "admin_pretraining_items.html",
        {"items": _items_of(db, PRACTICE_USE_TYPES), "result": result},
    )


@router.post("/pretraining-items/delete-all")
def admin_pretraining_items_delete_all(request: Request, db: Session = Depends(get_db)):
    redirect = _require_admin(request)
    if redirect:
        return redirect

    to_delete = db.query(Item).filter(Item.use_type.in_(PRACTICE_USE_TYPES)).all()
    deleted_count = len(to_delete)
    for item in to_delete:
        db.delete(item)
    db.commit()

    return templates.TemplateResponse(
        request,
        "admin_pretraining_items.html",
        {
            "items": _items_of(db, PRACTICE_USE_TYPES),
            "result": None,
            "delete_message": f"{deleted_count}개 문항을 삭제했습니다.",
        },
    )


