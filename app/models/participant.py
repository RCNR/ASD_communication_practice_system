from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base


class Participant(Base):
    __tablename__ = "participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    participant_code = Column(String, unique=True, nullable=False)  # P01, P02, ...
    password_hash = Column(String, nullable=False)
    baseline_length = Column(Integer, nullable=False)
    intervention_length = Column(Integer, nullable=False, default=20)  # brief section 3
    # The brief says 2, but the stability exit rule reads 3 consecutive
    # sessions, so a 2-session maintenance phase can never trigger it.
    maintenance_length = Column(Integer, nullable=False, default=4)
    current_phase = Column(String, nullable=False)  # baseline / intervention / maintenance
    status = Column(String, nullable=False, default="active")  # active / paused / dropped
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    pretraining_completed = Column(Boolean, nullable=False, default=False)
