"""조기 종료 규칙 변경(중재 최소 5회기, 기초선/유지 추세 기준 강화)을 배포한 직후
운영 DB에 한 번 실행하는 스크립트. 두 가지를 처리한다.

1. 종료 고정: 유지 단계를 옛 추세 기준으로 끝냈지만 새 기준에서는 다시 열리는
   참여자. 연구 완료 여부는 저장되지 않고 phase_complete()가 매번 다시 계산하므로,
   (12, 12, 15)처럼 끝난 참여자는 배포 후 유지 회기를 더 풀 수 있게 된다.
   maintenance_length를 완료한 유지 회기 수로 맞춰 "계획 회기 소진"으로 고정한다.

2. 중재 복귀: 옛 규칙으로 중재를 5회기 전에 끝내고 유지로 넘어갔지만 유지 회기를
   하나도 시작하지 않은 참여자. current_phase를 intervention으로 되돌린다. 다음 중재
   회기 번호는 기존 중재 회기 수 + 1로 이어지므로 이미 푼 세트는 다시 나오지 않는다.
   유지 회기를 시작했거나 끝낸 참여자는 유지 데이터를 보존하기 위해 건드리지 않는다.

사용법 (앱 루트에서, 참여자 접속이 없는 시간에 배포 직후):
    python scripts/apply_exit_rule_change.py           # 대상 목록만 출력
    python scripts/apply_exit_rule_change.py --apply   # 실제 반영
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.participant import Participant
from app.models.session import StudySession
from app.services.session_service import (
    INTERVENTION_MIN_SESSIONS,
    STABILITY_EXIT_STREAK,
    completed_session_count,
    phase_complete,
    session_score,
)


def old_stability_rule(scores: list[int]) -> bool:
    """변경 전 기초선/유지 기준: 동점은 방향을 깨지 않으므로 지그재그만 아니면 종료."""
    pairs = list(zip(scores, scores[1:]))
    return all(a <= b for a, b in pairs) or all(a >= b for a, b in pairs)


apply = "--apply" in sys.argv
db = SessionLocal()

to_pin = []  # (participant, 완료 유지 회기 수)
to_pin_blocked = []  # 이미 다시 열린 회기를 시작한 참여자 - 수동 확인 필요
to_rollback = []  # (participant, 완료 중재 회기 수)

for participant in db.query(Participant).filter_by(current_phase="maintenance").all():
    maintenance = (
        db.query(StudySession)
        .filter_by(participant_code=participant.participant_code, phase="maintenance")
        .order_by(StudySession.session_number)
        .all()
    )
    completed = [s for s in maintenance if s.status == "completed"]

    if not maintenance:
        intervention_done = completed_session_count(db, participant, "intervention")
        if intervention_done < min(INTERVENTION_MIN_SESSIONS, participant.intervention_length):
            to_rollback.append((participant, intervention_done))
        continue

    if len(completed) < STABILITY_EXIT_STREAK or phase_complete(db, participant):
        continue
    # 옛 규칙으로 끝난 참여자는 그 뒤로 회기가 생길 수 없었으므로, 마지막 완료 3회기가
    # 옛 기준을 충족하면 옛 규칙에서 연구가 끝난 상태였다는 뜻이다.
    last_scores = [session_score(db, s)[0] for s in completed[-STABILITY_EXIT_STREAK:]]
    if not old_stability_rule(last_scores):
        continue
    if len(completed) != len(maintenance):
        to_pin_blocked.append((participant, last_scores))
    else:
        to_pin.append((participant, len(completed), last_scores))

print("[1] 종료 고정 (maintenance_length를 완료 회기 수로)")
for participant, done, scores in to_pin:
    print(
        f"  {participant.participant_code}\t유지 완료 {done}회기 (최근 {scores})\t"
        f"maintenance_length {participant.maintenance_length} -> {done}"
    )
for participant, scores in to_pin_blocked:
    print(
        f"  !! {participant.participant_code}\t최근 {scores}로 끝났지만 배포 후 새 유지 회기를 "
        "이미 시작함 - 자동 처리하지 않음, 수동 확인 필요"
    )
print(f"  대상 {len(to_pin)}명, 수동 확인 {len(to_pin_blocked)}명")

print("[2] 중재 복귀 (current_phase -> intervention)")
for participant, done in to_rollback:
    print(f"  {participant.participant_code}\tstatus={participant.status}\t중재 완료 {done}회기")
print(f"  대상 {len(to_rollback)}명")

if not apply:
    print("dry-run: --apply로 반영")
elif to_pin or to_rollback:
    for participant, done, _ in to_pin:
        participant.maintenance_length = done
    for participant, _ in to_rollback:
        participant.current_phase = "intervention"
    db.commit()
    print("반영 완료")

db.close()
