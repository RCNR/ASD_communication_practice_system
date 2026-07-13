from sqlalchemy import Column, Float, String

from app.core.database import Base


class Item(Base):
    __tablename__ = "items"

    item_id = Column(String, primary_key=True)  # INT_001, ASM_001, ...
    use_type = Column(String, nullable=False)  # intervention / assessment / pilot
    ssis_domain = Column(String, nullable=True)  # SSIS영역
    situation_type = Column(String, nullable=True)
    sub_response_type = Column(String, nullable=True)  # 하위반응유형
    difficulty = Column(String, nullable=True)  # 난이도
    primary_dv = Column(String, nullable=True)  # 주요종속변인
    emotion_tag = Column(String, nullable=True)
    item_text = Column(String, nullable=False)
    target_response = Column(String, nullable=True)
    hint_template = Column(String, nullable=True)
    example_score_2 = Column(String, nullable=True)  # 2점 적합 답안 예시
    example_score_1 = Column(String, nullable=True)  # 1점 부분 답안 예시
    example_score_0 = Column(String, nullable=True)  # 0점 부적합 답안 예시
    scoring_criteria = Column(String, nullable=True)  # 채점기준요약 - AI 채점 프롬프트에 전달
    cvi_score = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="approved")  # approved / revise / deleted
