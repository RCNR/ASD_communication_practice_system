"""지정한 참여자의 유지 회기를 무효(stopped)로 하고 중재 단계로 되돌리는 1회성 스크립트.

중재 최소 회기(INTERVENTION_MIN_SESSIONS) 도입 전에 중재를 조기 종료하고 유지로
넘어간 참여자가 대상이다. 응답·채점 기록은 지우지 않고 회기 상태만 stopped로 바꾼다.
무효 회기는 회기 번호·완료 수·종료 판정에서 빠지므로, 중재를 마치고 유지에 다시
들어오면 유지 1회기(세트 11번)부터 새로 시작한다.

중재는 기존 중재 회기 수 + 1부터 이어지므로 이미 푼 중재 세트는 다시 나오지 않는다.

사용법 (앱 루트에서, 대상자가 접속하지 않는 시간에):
    python scripts/reopen_intervention.py 코드1 코드2 ...                 # 대상 확인
    python scripts/reopen_intervention.py 코드1 코드2 ... --apply         # 반영
    --maintenance-length N  : 대상자의 유지 계획 회기 수도 N으로 맞춘다
                              (apply_exit_rule_change.py가 줄여 둔 경우 원래 값으로 복원)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.participant import Participant
from app.models.session import StudySession
from app.services.session_service import INTERVENTION_MIN_SESSIONS, completed_session_count

args = sys.argv[1:]
apply = "--apply" in args
maintenance_length = None
if "--maintenance-length" in args:
    maintenance_length = int(args[args.index("--maintenance-length") + 1])
codes = [
    a for i, a in enumerate(args)
    if not a.startswith("--") and (i == 0 or args[i - 1] != "--maintenance-length")
]
if not codes:
    sys.exit(__doc__)

db = SessionLocal()
targets = []  # (participant, 무효 처리할 유지 회기 목록)

for code in codes:
    participant = db.query(Participant).filter_by(participant_code=code).first()
    if participant is None:
        print(f"  건너뜀 {code}: 참여자 없음")
        continue
    if participant.current_phase != "maintenance":
        print(f"  건너뜀 {code}: 현재 단계가 {participant.current_phase}")
        continue
    intervention_done = completed_session_count(db, participant, "intervention")
    if intervention_done >= INTERVENTION_MIN_SESSIONS:
        print(f"  건너뜀 {code}: 중재를 이미 {intervention_done}회기 완료")
        continue

    maintenance = (
        db.query(StudySession)
        .filter_by(participant_code=code, phase="maintenance")
        .filter(StudySession.status != "stopped")
        .order_by(StudySession.session_number)
        .all()
    )
    targets.append((participant, maintenance))
    sessions_text = ", ".join(f"{s.session_number}회기({s.status})" for s in maintenance) or "없음"
    length_text = (
        f"\t유지 계획 {participant.maintenance_length} -> {maintenance_length}"
        if maintenance_length is not None and maintenance_length != participant.maintenance_length
        else ""
    )
    print(
        f"  {code}\t중재 완료 {intervention_done}회기 -> 다음 중재 {intervention_done + 1}회기\t"
        f"무효 처리할 유지: {sessions_text}{length_text}"
    )

print(f"대상 {len(targets)}명" + ("" if apply else " (dry-run: --apply로 반영)"))

if apply and targets:
    for participant, maintenance in targets:
        for study_session in maintenance:
            study_session.status = "stopped"
        participant.current_phase = "intervention"
        if maintenance_length is not None:
            participant.maintenance_length = maintenance_length
    db.commit()
    print("반영 완료")

db.close()
