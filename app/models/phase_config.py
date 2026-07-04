from sqlalchemy import Boolean, Column, Integer, String

from app.core.database import Base


class PhaseConfig(Base):
    __tablename__ = "phase_configs"

    phase = Column(String, primary_key=True)  # baseline / intervention / maintenance
    ai_hint_enabled = Column(Boolean, nullable=False, default=False)
    default_item_count = Column(Integer, nullable=False, default=6)
