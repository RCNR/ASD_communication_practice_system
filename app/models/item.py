from sqlalchemy import Column, Integer, String

from app.core.database import Base


class Item(Base):
    __tablename__ = "items"

    item_id = Column(String, primary_key=True)  # Q001, Q002, ...
    # assessment(기초선/유지) / intervention(중재) /
    # practice_assessment, practice_intervention(사전교육)
    use_type = Column(String, nullable=False)
    # 회기 번호. 그 참여자의 해당 단계 내 순번이며, assessment는 1~10이 기초선
    # 1~10회기, 11~15가 유지 1~5회기다 (session_service.MAINTENANCE_SET_NO_OFFSET).
    set_no = Column(Integer, nullable=False)
    # 회기 내 제시 순서. 홀수=positive, 짝수=negative로 문항은행에서 이미 맞춰져
    # 있으므로 이 순서를 그대로 제시하면 회기당 정서 균형이 자동으로 맞는다.
    set_order = Column(Integer, nullable=False)
    sentiment = Column(String, nullable=True)  # positive / negative - item_text의 정서 특징
    item_text = Column(String, nullable=False)
    example_score_2 = Column(String, nullable=True)  # 2점 예시 (인정 + 이어가기) - 3단계에서 그대로 노출
    # 아래 3개는 연구자용 표본이라 저장만 한다. 화면에도, AI 프롬프트에도 넣지 않는다
    # (채점에 넣으면 "예시와 비슷한가"로 판단하게 되고, 힌트에 넣으면 정답이 샌다).
    example_score_1_ack = Column(String, nullable=True)  # 1점 예시 - 인정(acknowledge)만
    example_score_1_con = Column(String, nullable=True)  # 1점 예시 - 이어가기(continue)만
    example_score_0 = Column(String, nullable=True)  # 0점 예시
    # 힌트 생성 AI에 넘기는 재료. 중재용(intervention/practice_intervention) 문항에만 값이 들어감.
    hint_ack = Column(String, nullable=True)  # 알아줄 정서
    hint_con = Column(String, nullable=True)  # 이어갈 방향
    hint_fallback = Column(String, nullable=True)  # AI 호출 실패 시 힌트 자리에 그대로 출력할 문구
    status = Column(String, nullable=False, default="approved")  # approved / revise / deleted - xlsx 컬럼 아님, 내부용
