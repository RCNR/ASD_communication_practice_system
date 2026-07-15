from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class TrialResponse(Base):
    __tablename__ = "trial_responses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    item_id = Column(String, ForeignKey("items.item_id"), nullable=False)
    phase = Column(String, nullable=False)  # baseline / intervention / maintenance
    item_order = Column(Integer, nullable=False)
    first_response = Column(String, nullable=True)
    first_response_started_at = Column(DateTime(timezone=True), nullable=True)
    first_response_submitted_at = Column(DateTime(timezone=True), nullable=True)
    revised_response_1 = Column(String, nullable=True)
    revised_response_2 = Column(String, nullable=True)
    final_response = Column(String, nullable=True)
    example_used = Column(Boolean, nullable=False, default=False)
    completed = Column(Boolean, nullable=False, default=False)
    safety_flag = Column(String, nullable=True)  # self_harm / violence / abuse / privacy / inappropriate / sexual
    safety_rewrite_count = Column(Integer, nullable=False, default=0)
