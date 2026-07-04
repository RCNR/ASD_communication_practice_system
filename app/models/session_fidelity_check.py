from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class SessionFidelityCheck(Base):
    """Reviewer checklist for a single intervention session's AI hint usage
    (brief section 11). Fields are nullable=True: None means not yet reviewed."""

    __tablename__ = "session_fidelity_checks"

    session_id = Column(Integer, ForeignKey("sessions.id"), primary_key=True)

    hint_matches_item = Column(Boolean, nullable=True)  # 지정된 문항에 대한 힌트였는가
    no_new_situation = Column(Boolean, nullable=True)  # 새로운 상황을 만들지 않았는가
    no_scoring = Column(Boolean, nullable=True)  # 정답/오답을 말하지 않았는가
    no_points_given = Column(Boolean, nullable=True)  # 점수를 주지 않았는가
    no_full_answer_given = Column(Boolean, nullable=True)  # 완성 답장을 먼저 제공하지 않았는가
    no_personal_info_request = Column(Boolean, nullable=True)  # 개인정보를 요구하지 않았는가
    no_risky_advice = Column(Boolean, nullable=True)  # 위험하거나 부적절한 조언이 없었는가
    hint_level_respected = Column(Boolean, nullable=True)  # 1단계/2단계 힌트 범위를 지켰는가

    note = Column(String, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
