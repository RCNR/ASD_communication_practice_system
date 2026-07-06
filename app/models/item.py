from sqlalchemy import Column, Float, String

from app.core.database import Base


class Item(Base):
    __tablename__ = "items"

    item_id = Column(String, primary_key=True)  # INT_001, ASM_001, ...
    use_type = Column(String, nullable=False)  # intervention / assessment / pilot
    situation_type = Column(String, nullable=True)
    emotion_tag = Column(String, nullable=True)
    item_text = Column(String, nullable=False)
    target_response = Column(String, nullable=True)
    hint_template = Column(String, nullable=True)
    verified_example = Column(String, nullable=True)
    cvi_score = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="approved")  # approved / revise / deleted
