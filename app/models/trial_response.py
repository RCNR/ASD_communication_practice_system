from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class TrialResponse(Base):
    __tablename__ = "trial_responses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    item_id = Column(String, ForeignKey("items.item_id"), nullable=False)
    phase = Column(String, nullable=False)  # baseline / intervention / maintenance
    item_order = Column(Integer, nullable=False)
    # The literal first thing the participant ever submitted for this trial's
    # "첫 답장" step, saved unconditionally (even if it fails validity/safety/
    # profanity checks and gets rejected). Unlike first_response below, this
    # is never overwritten once set - it exists purely so a retry loop can't
    # erase what the participant's true first attempt looked like.
    first_attempt_response = Column(String, nullable=True)
    first_response = Column(String, nullable=True)
    first_response_started_at = Column(DateTime(timezone=True), nullable=True)
    first_response_submitted_at = Column(DateTime(timezone=True), nullable=True)
    revised_response_1 = Column(String, nullable=True)
    revised_response_2 = Column(String, nullable=True)
    final_response = Column(String, nullable=True)
    example_used = Column(Boolean, nullable=False, default=False)
    completed = Column(Boolean, nullable=False, default=False)
