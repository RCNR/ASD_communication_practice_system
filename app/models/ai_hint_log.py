from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base


class AiHintLog(Base):
    __tablename__ = "ai_hint_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trial_responses.id"), nullable=False)
    hint_level = Column(Integer, nullable=False)  # 1 or 2
    prompt_payload = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    api_response_raw = Column(String, nullable=True)
    hint_message = Column(String, nullable=True)
    fallback_used = Column(Boolean, nullable=False, default=False)
    contains_scoring = Column(Boolean, nullable=False, default=False)
    contains_full_answer = Column(Boolean, nullable=False, default=False)
    safety_flag = Column(String, nullable=False, default="none")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
