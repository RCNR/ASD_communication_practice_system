from sqlalchemy import Column, ForeignKey, Integer, String

from app.core.database import Base


class SessionItem(Base):
    """Explicit per-session item assignment, so baseline/intervention/maintenance
    sessions can each be given a different set of items, not just a different count."""

    __tablename__ = "session_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    item_id = Column(String, ForeignKey("items.item_id"), nullable=False)
    item_order = Column(Integer, nullable=False)
