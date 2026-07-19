from sqlalchemy import Column, Integer, String

from app.core.database import Base


class PhaseConfig(Base):
    __tablename__ = "phase_configs"

    phase = Column(String, primary_key=True)  # baseline / intervention / maintenance
    default_item_count = Column(Integer, nullable=False, default=6)
