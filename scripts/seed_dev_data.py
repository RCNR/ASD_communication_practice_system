import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal, init_db
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

db.commit()
db.close()
print("Seed complete.")
