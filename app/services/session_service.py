from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.participant import Participant
from app.models.phase_config import PhaseConfig
from app.models.session import StudySession
from app.models.session_item import SessionItem
from app.models.trial_response import TrialResponse

PHASE_USE_TYPE = {
    "baseline": "assessment",
    "intervention": "intervention",
    "maintenance": "assessment",
}

PHASE_ORDER = ["baseline", "intervention", "maintenance"]


def get_target_session_count(participant: Participant) -> int:
    return {
        "baseline": participant.baseline_length,
        "intervention": participant.intervention_length,
        "maintenance": participant.maintenance_length,
    }[participant.current_phase]


def completed_session_count(db: DbSession, participant: Participant) -> int:
    return (
        db.query(StudySession)
        .filter_by(
            participant_code=participant.participant_code,
            phase=participant.current_phase,
            status="completed",
        )
        .count()
    )


def advance_phase_if_needed(db: DbSession, participant: Participant) -> None:
    target = get_target_session_count(participant)
    completed = completed_session_count(db, participant)
    if completed < target:
        return

    current_index = PHASE_ORDER.index(participant.current_phase)
    if current_index < len(PHASE_ORDER) - 1:
        participant.current_phase = PHASE_ORDER[current_index + 1]
        db.commit()


def get_wait_seconds(participant: Participant, phase: str) -> int:
    return {
        "baseline": participant.baseline_wait_seconds,
        "intervention": participant.intervention_wait_seconds,
        "maintenance": participant.maintenance_wait_seconds,
    }[phase]


def check_wait_gate(db: DbSession, participant: Participant) -> datetime | None:
    """Returns the datetime the next session becomes available, or None if the
    participant can start one right now (already in progress, first-ever
    session, or the configured wait period has already elapsed)."""
    phase = participant.current_phase

    active_session = (
        db.query(StudySession)
        .filter_by(participant_code=participant.participant_code, phase=phase, status="in_progress")
        .first()
    )
    if active_session:
        return None

    last_completed = (
        db.query(StudySession)
        .filter_by(participant_code=participant.participant_code, status="completed")
        .order_by(StudySession.completed_at.desc())
        .first()
    )
    if last_completed is None or last_completed.completed_at is None:
        return None

    completed_at = last_completed.completed_at
    if completed_at.tzinfo is None:
        # SQLite drops tzinfo on read even for DateTime(timezone=True) columns.
        completed_at = completed_at.replace(tzinfo=timezone.utc)

    wait_seconds = get_wait_seconds(participant, phase)
    available_at = completed_at + timedelta(seconds=wait_seconds)
    if available_at <= datetime.now(timezone.utc):
        return None
    return available_at


def get_active_session(db: DbSession, participant: Participant) -> StudySession | None:
    return (
        db.query(StudySession)
        .filter_by(
            participant_code=participant.participant_code,
            phase=participant.current_phase,
            status="in_progress",
        )
        .first()
    )


def get_next_session_number(db: DbSession, participant: Participant) -> int:
    return (
        db.query(StudySession)
        .filter_by(participant_code=participant.participant_code, phase=participant.current_phase)
        .count()
        + 1
    )


def _ensure_assignment_order(db: DbSession, use_type: str) -> None:
    """Assigns a shared draw order to any approved items of this use_type
    that don't have one yet. New items are shuffled in after the current
    tail rather than reshuffling everything, so previously-assigned
    positions (and therefore what session N means for existing items) never
    move once set."""
    unordered = (
        db.query(Item)
        .filter_by(use_type=use_type, status="approved")
        .filter(Item.assignment_order.is_(None))
        .all()
    )
    if not unordered:
        return

    current_max = (
        db.query(func.max(Item.assignment_order)).filter_by(use_type=use_type, status="approved").scalar()
    ) or 0

    random.shuffle(unordered)
    for offset, item in enumerate(unordered, start=1):
        item.assignment_order = current_max + offset
    db.commit()


def get_or_create_active_session(db: DbSession, participant: Participant) -> StudySession | None:
    phase = participant.current_phase

    active_session = get_active_session(db, participant)
    if active_session:
        return active_session

    target = get_target_session_count(participant)
    if completed_session_count(db, participant) >= target:
        # Terminal phase (maintenance) already completed its full session count.
        return None

    session_number = (
        db.query(StudySession)
        .filter_by(participant_code=participant.participant_code, phase=phase)
        .count()
        + 1
    )
    phase_config = db.get(PhaseConfig, phase)
    planned_item_count = phase_config.default_item_count if phase_config else 6

    new_session = StudySession(
        participant_code=participant.participant_code,
        phase=phase,
        session_number=session_number,
        planned_item_count=planned_item_count,
        status="in_progress",
        started_at=datetime.now(timezone.utc),
    )
    db.add(new_session)
    db.flush()

    use_type = PHASE_USE_TYPE[phase]
    _ensure_assignment_order(db, use_type)

    ordered_pool = (
        db.query(Item)
        .filter_by(use_type=use_type, status="approved")
        .order_by(Item.assignment_order)
        .all()
    )

    # Session N always draws the same slice of the shared order, so every
    # participant sees identical items for the same session_number (within
    # this phase - baseline and maintenance each count from 1 independently
    # even though they share the assessment pool). Once the pool is
    # exhausted, later sessions wrap back to the start of the same order
    # rather than reshuffling, so the guarantee holds indefinitely.
    n = len(ordered_pool)
    count = min(planned_item_count, n)
    start = ((session_number - 1) * planned_item_count) % n if n else 0
    candidate_items = [ordered_pool[(start + i) % n] for i in range(count)]

    for order, item in enumerate(candidate_items, start=1):
        db.add(SessionItem(session_id=new_session.id, item_id=item.item_id, item_order=order))
        db.add(
            TrialResponse(
                session_id=new_session.id,
                item_id=item.item_id,
                phase=phase,
                item_order=order,
                completed=False,
            )
        )
    db.commit()
    db.refresh(new_session)
    return new_session


def get_current_trial(db: DbSession, study_session: StudySession) -> TrialResponse | None:
    return (
        db.query(TrialResponse)
        .filter_by(session_id=study_session.id, completed=False)
        .order_by(TrialResponse.item_order)
        .first()
    )


def mark_session_completed(db: DbSession, study_session: StudySession) -> None:
    study_session.status = "completed"
    study_session.completed_at = datetime.now(timezone.utc)
    db.commit()


def get_latest_hint_message(db: DbSession, trial_id: int, hint_level: int) -> str | None:
    log = (
        db.query(AiHintLog)
        .filter_by(trial_id=trial_id, hint_level=hint_level)
        .order_by(AiHintLog.created_at.desc())
        .first()
    )
    return log.hint_message if log else None


def get_latest_evaluation(db: DbSession, trial_id: int, hint_level: int) -> AiHintLog | None:
    return (
        db.query(AiHintLog)
        .filter_by(trial_id=trial_id, hint_level=hint_level)
        .order_by(AiHintLog.created_at.desc())
        .first()
    )
