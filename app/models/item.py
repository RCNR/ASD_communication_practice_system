from sqlalchemy import Column, String

from app.core.database import Base


class Item(Base):
    __tablename__ = "items"

    item_id = Column(String, primary_key=True)  # INT_001, ASM_001, ...
    use_type = Column(String, nullable=False)  # intervention / assessment / pilot
    sentiment = Column(String, nullable=True)  # positive / negative - item_text의 정서 특징
    item_text = Column(String, nullable=False)
    example_score_2 = Column(String, nullable=True)  # 2점 예시 (인정 + 이어가기)
    example_score_1_ack = Column(String, nullable=True)  # 1점 예시 - 인정(acknowledge)만
    example_score_1_con = Column(String, nullable=True)  # 1점 예시 - 이어가기(continue)만
    example_score_0 = Column(String, nullable=True)  # 0점 예시
    hint_template = Column(String, nullable=True)  # intervention 문항에만 값이 들어감
    status = Column(String, nullable=False, default="approved")  # approved / revise / deleted - xlsx 컬럼 아님, 내부용
