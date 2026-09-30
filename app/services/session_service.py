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

# 중재 단계는 숙달 기준을 충족해도 최소 이 회기 수를 채우기 전에는 끝나지 않는다.
# 연구 분석에 필요한 하한이다. 5회기를 채운 뒤부터 기존 연속 3회기 판정을 한다
# (5회기 직후의 판정 창은 3·4·5회기).
INTERVENTION_MIN_SESSIONS = 5

# 기초선/유지 단계 조기 종료(안정성) 기준. 연속 STABILITY_EXIT_STREAK 회기의
# 점수가 매 회기 오르거나(우상향) 매 회기 내리거나(우하향) 전부 같으면(변동
# 없음) 남은 회기를 실시하지 않는다. 가운데 점수에서 방향이 정해지면 세 번째도
# 같은 방향이어야 하므로 (12, 12, 15)나 (15, 12, 12)처럼 한 번만 같은 경우는
# 종료하지 않는다.
STABILITY_EXIT_STREAK = 3


def get_target_session_count(participant: Participant) -> int:
    return {
        "baseline": participant.baseline_length,
        "intervention": participant.intervention_length,
        "maintenance": participant.maintenance_length,
    }[participant.current_phase]


def completed_session_count(
    db: DbSession, participant: Participant, phase: str | None = None
) -> int:
    return (
        db.query(StudySession)
        .filter_by(
            participant_code=participant.participant_code,
            phase=phase or participant.current_phase,
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


def _recent_completed_sessions(
    db: DbSession, participant: Participant, count: int
) -> list[StudySession]:
    """현재 단계에서 가장 최근에 완료한 회기 count개를 회기 번호 순으로.
    완료 회기가 count개에 못 미치면 [] - 아직 판정할 수 없다는 뜻이다."""
    sessions = (
        db.query(StudySession)
        .filter_by(
            participant_code=participant.participant_code,
            phase=participant.current_phase,
            status="completed",
        )
        .order_by(StudySession.session_number)
        .all()
    )
    return sessions[-count:] if len(sessions) >= count else []


def meets_mastery(scores: list[tuple[int, int]]) -> bool:
    """중재 숙달 판정. scores는 회기별 (획득 점수, 만점)."""
    return all(
        possible > 0 and earned >= possible * INTERVENTION_EXIT_RATIO
        for earned, possible in scores
    )


def is_monotonic(scores: list[int]) -> bool:
    """기초선/유지 안정성 판정: 점수가 매 회기 오르거나, 매 회기 내리거나,
    전부 같은지. (12, 12, 15)처럼 중간에 한 번만 같은 경우는 False."""
    return trend_label(scores) != "지그재그"


def trend_label(scores: list[int]) -> str:
    """관리자 점수판에 표시할 추세 이름. is_monotonic이 False인 구간은 모두
    '지그재그'로 묶는다 ((12, 12, 15)처럼 방향이 이어지지 않는 경우 포함)."""
    if len(set(scores)) == 1:
        return "변동 없음"
    pairs = list(zip(scores, scores[1:]))
    if all(a < b for a, b in pairs):
        return "우상향"
    if all(a > b for a, b in pairs):
        return "우하향"
    return "지그재그"


def has_mastery_streak(db: DbSession, participant: Participant) -> bool:
    """중재 단계 숙달 기준: 가장 최근 완료 회기 3개가 연속으로 75% 이상인지.
    회기 번호 순으로 보므로 중간에 기준 미달 회기가 있으면 연속이 끊긴다."""
    sessions = _recent_completed_sessions(db, participant, INTERVENTION_EXIT_STREAK)
    if not sessions:
        return False
    return meets_mastery([session_score(db, s) for s in sessions])


def has_stability_streak(db: DbSession, participant: Participant) -> bool:
    """기초선/유지 단계 안정성 기준: 가장 최근 완료 회기 3개의 점수가 한 방향으로
    움직이거나(우상향/우하향) 변하지 않는지."""
    sessions = _recent_completed_sessions(db, participant, STABILITY_EXIT_STREAK)
    if not sessions:
        return False
    return is_monotonic([session_score(db, s)[0] for s in sessions])


def phase_complete(db: DbSession, participant: Participant) -> bool:
    """현재 단계에서 더 실시할 회기가 남아 있지 않은지. 계획된 회기 수를 모두
    채웠거나, 단계별 조기 종료 기준을 충족하면 True.

    유지 단계에서 True가 되면 PHASE_ORDER에 다음 단계가 없으므로 그대로 연구
    종료가 된다 - get_or_create_active_session이 더 이상 회기를 만들지 않고
    study_complete 화면이 뜬다."""
    completed = completed_session_count(db, participant)
    if completed >= get_target_session_count(participant):
        return True
    if participant.current_phase == "intervention":
        if completed < INTERVENTION_MIN_SESSIONS:
            return False
        return has_mastery_streak(db, participant)
    return has_stability_streak(db, participant)


def advance_phase_if_needed(db: DbSession, participant: Participant) -> None:
    """단계가 끝났으면 다음 단계로 넘긴다. 남은 회기를 따로 지울 필요는 없다:
    current_phase가 바뀌면 get_or_create_active_session이 더 이상 이전 단계의
    회기를 만들지 않고, completed_session_count도 새 단계 기준으로 다시 센다."""
    if not phase_complete(db, participant):
        return

    current_index = PHASE_ORDER.index(participant.current_phase)
    if current_index < len(PHASE_ORDER) - 1:
        participant.current_phase = PHASE_ORDER[current_index + 1]
        db.commit()


def phase_ended_early(db: DbSession, participant: Participant, phase: str) -> bool:
    """해당 단계가 계획된 회기를 다 채우지 않고 끝났는지. 이미 지나간 단계이거나,
    현재 단계인데 종료 조건을 충족한 경우에만 True."""
    target = {
        "baseline": participant.baseline_length,
        "intervention": participant.intervention_length,
        "maintenance": participant.maintenance_length,
    }[phase]
    if completed_session_count(db, participant, phase) >= target:
        return False

    phase_index = PHASE_ORDER.index(phase)
    current_index = PHASE_ORDER.index(participant.current_phase)
    if phase_index < current_index:
        return True
    if phase_index == current_index:
        return phase_complete(db, participant)
    return False


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

    if phase_complete(db, participant):
        # Terminal phase (maintenance) is over - either it ran its full session
        # count or it met the stability criterion.
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
