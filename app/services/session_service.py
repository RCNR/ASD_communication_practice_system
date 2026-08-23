from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session as DbSession

from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.participant import Participant
from app.models.session import StudySession
from app.models.session_item import SessionItem
from app.models.trial_response import TrialResponse

PHASE_USE_TYPE = {
    "baseline": "assessment",
    "intervention": "intervention",
    "maintenance": "assessment",
}

PHASE_ORDER = ["baseline", "intervention", "maintenance"]

# 기초선과 유지는 같은 assessment 문항은행을 쓰면서 회기 번호를 각각 1부터 세므로,
# 유지 회기의 set_no는 기초선 구간(1~10) 다음인 11부터 시작하도록 밀어준다.
# 참여자마다 기초선 길이가 다른 중다기초선 설계여도 이 오프셋은 고정이다 - 기초선을
# 3회기만 한 참여자는 set_no 4~10을 쓰지 않고 건너뛴다.
MAINTENANCE_SET_NO_OFFSET = 10

# 중재 단계 조기 종료(숙달) 기준. 연속 INTERVENTION_EXIT_STREAK 회기가 각각
# 만점의 INTERVENTION_EXIT_RATIO 이상이면 남은 중재 회기를 실시하지 않고
# 유지 단계로 넘어간다. 한 세트 10문항 x 2점 = 20점이므로 지금 데이터에서는
# 회기당 15점이 기준선이다. 문항 수가 바뀌어도 규칙이 깨지지 않도록 고정
# 점수가 아니라 비율로 판정한다.
INTERVENTION_EXIT_STREAK = 3
INTERVENTION_EXIT_RATIO = 0.75


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


def measured_score(trial: TrialResponse, eval_log) -> int | None:
    """The score used for research measurement/display. Forced to 0 if the
    participant's true independent first attempt (first_attempt_response)
    was rejected by the validity/safety/profanity gate before an acceptable
    answer was reached (i.e. it differs from what actually got saved into
    first_response) - regardless of what that accepted retry's own AI
    judgment (eval_log.score_level) came out to.

    This never touches AiHintLog.score_level itself, which still drives the
    intervention hint/pass flow honestly (see student.py's
    session_first_response) - only what gets summed here."""
    if eval_log is None:
        return None
    if (
        trial.first_attempt_response is not None
        and trial.first_response is not None
        and trial.first_attempt_response != trial.first_response
    ):
        return 0
    return eval_log.score_level


def session_score(db: DbSession, study_session: StudySession) -> tuple[int, int]:
    """(획득 점수, 만점) for one session, from the first-response (독립 반응)
    scores - the same numbers the admin score table shows. Max is 문항 수 x 2.

    A trial with no AI evaluation logged contributes 0 to the earned score but
    still counts toward the max, so a session that failed to score can never
    pass the mastery threshold by shrinking its own denominator."""
    trials = db.query(TrialResponse).filter_by(session_id=study_session.id).all()
    earned = 0
    for trial in trials:
        score = measured_score(trial, get_latest_evaluation(db, trial.id, 1))
        if score is not None:
            earned += score
    return earned, len(trials) * 2


def has_mastery_streak(db: DbSession, participant: Participant) -> bool:
    """중재 단계에서 가장 최근 완료 회기 3개가 연속으로 75% 이상인지.
    회기 번호 순으로 보므로 중간에 기준 미달 회기가 있으면 연속이 끊긴다."""
    sessions = (
        db.query(StudySession)
        .filter_by(
            participant_code=participant.participant_code,
            phase="intervention",
            status="completed",
        )
        .order_by(StudySession.session_number)
        .all()
    )
    if len(sessions) < INTERVENTION_EXIT_STREAK:
        return False

    for study_session in sessions[-INTERVENTION_EXIT_STREAK:]:
        earned, possible = session_score(db, study_session)
        if possible == 0 or earned < possible * INTERVENTION_EXIT_RATIO:
            return False
    return True


def advance_phase_if_needed(db: DbSession, participant: Participant) -> None:
    if participant.current_phase == "intervention" and has_mastery_streak(db, participant):
        # 숙달 기준 충족 - 남은 중재 회기는 실시하지 않는다. 남은 회기를 따로
        # 지울 필요는 없다: current_phase가 바뀌면 get_or_create_active_session이
        # 더 이상 중재 회기를 만들지 않고, completed_session_count도 유지 단계
        # 기준으로 다시 세므로 유지 1회기부터 시작된다.
        participant.current_phase = "maintenance"
        db.commit()
        return

    target = get_target_session_count(participant)
    completed = completed_session_count(db, participant)
    if completed < target:
        return

    current_index = PHASE_ORDER.index(participant.current_phase)
    if current_index < len(PHASE_ORDER) - 1:
        participant.current_phase = PHASE_ORDER[current_index + 1]
        db.commit()


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


def get_set_no(phase: str, session_number: int) -> int:
    """Which set_no in the item bank this phase's session_number maps to."""
    if phase == "maintenance":
        return session_number + MAINTENANCE_SET_NO_OFFSET
    return session_number


def get_set_items(db: DbSession, phase: str, session_number: int) -> list[Item]:
    """The item bank rows for one session, in the order they must be shown.
    No shuffling: set_order already alternates positive/negative so that
    presenting it as-is keeps each session's sentiment balance.

    Returns [] when the session number falls outside the phase's set_no range,
    which the caller surfaces as "문항이 준비되지 않았어요". Without this bound a
    participant given more than MAINTENANCE_SET_NO_OFFSET baseline sessions
    would silently start drawing the maintenance sets."""
    if phase == "baseline" and session_number > MAINTENANCE_SET_NO_OFFSET:
        return []

    return (
        db.query(Item)
        .filter_by(
            use_type=PHASE_USE_TYPE[phase],
            set_no=get_set_no(phase, session_number),
            status="approved",
        )
        .order_by(Item.set_order)
        .all()
    )


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

    set_items = get_set_items(db, phase, session_number)
    if not set_items:
        # The item bank has no set for this session number (xlsx not uploaded
        # yet, or the participant was given more sessions than the bank
        # covers). Creating an empty session would immediately "complete" it
        # and advance the participant past a session they never took, so
        # refuse instead and let the caller show a notice.
        return None

    new_session = StudySession(
        participant_code=participant.participant_code,
        phase=phase,
        session_number=session_number,
        planned_item_count=len(set_items),
        status="in_progress",
        started_at=datetime.now(timezone.utc),
    )
    db.add(new_session)
    db.flush()

    for order, item in enumerate(set_items, start=1):
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
