import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal, init_db
from app.models.item import Item
from app.models.participant import Participant
from app.models.phase_config import PhaseConfig

init_db()
db = SessionLocal()

db.merge(
    Participant(
        participant_code="P01",
        password_hash=hashlib.sha256(b"1234").hexdigest(),
        baseline_length=3,
        current_phase="baseline",
        status="active",
    )
)

# Not a real research participant code - only for browsing the intervention
# screen in dev without disturbing P01's baseline data.
db.merge(
    Participant(
        participant_code="P_TEST",
        password_hash=hashlib.sha256(b"1234").hexdigest(),
        baseline_length=3,
        current_phase="intervention",
        status="active",
    )
)

for phase, ai_hint_enabled, default_item_count in [
    ("baseline", False, 6),
    ("intervention", True, 10),
    ("maintenance", False, 6),
]:
    db.merge(
        PhaseConfig(
            phase=phase,
            ai_hint_enabled=ai_hint_enabled,
            default_item_count=default_item_count,
        )
    )

items = [
    Item(
        item_id="ASM_001",
        use_type="assessment",
        situation_type="부정적 감정 표현",
        emotion_tag="부정",
        item_text="나 오늘 발표하다가 말이 꼬여서 너무 창피했어.",
        target_response="감정 인정, 위로, 질문",
        status="approved",
    ),
    Item(
        item_id="ASM_002",
        use_type="assessment",
        situation_type="긍정적 사건 공유",
        emotion_tag="긍정",
        item_text="나 오늘 수행평가 만점 받았어!",
        target_response="감정 인정, 축하",
        status="approved",
    ),
    Item(
        item_id="INT_001",
        use_type="intervention",
        situation_type="부정적 감정 표현",
        emotion_tag="부정",
        item_text="나 오늘 발표하다가 말이 꼬여서 너무 창피했어.",
        target_response="감정 인정, 위로, 질문",
        hint_template="친구가 지금 어떤 마음일지 생각해보자.",
        example_score_2="많이 창피했겠다. 그래도 끝까지 발표한 건 대단해. 어떤 부분이 제일 어려웠어?",
        example_score_1="발표하다가 그랬구나.",
        example_score_0="나는 발표 안 했는데.",
        cvi_score=0.83,
        status="approved",
    ),
    Item(
        item_id="INT_002",
        use_type="intervention",
        situation_type="도움 요청",
        emotion_tag="중립",
        item_text="나 이번 숙제 너무 어려운데 어떻게 해야 할지 모르겠어.",
        target_response="공감, 도움 제안",
        hint_template="친구가 어떤 도움이 필요할지 생각해보자.",
        example_score_2="많이 막막하겠다. 어디서부터 어려운지 같이 보면서 도와줄까?",
        example_score_1="숙제가 어렵구나.",
        example_score_0="나는 숙제 다 했어.",
        cvi_score=0.83,
        status="approved",
    ),
]
for item in items:
    db.merge(item)

db.commit()
db.close()
print("Seed complete.")
