from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class StudySession(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    participant_code = Column(String, ForeignKey("participants.participant_code"), nullable=False)
    phase = Column(String, nullable=False)  # baseline / intervention / maintenance
    session_number = Column(Integer, nullable=False)
    planned_item_count = Column(Integer, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, nullable=False, default="scheduled")  # scheduled / in_progress / completed / stopped
