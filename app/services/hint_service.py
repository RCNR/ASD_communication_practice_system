from __future__ import annotations

import json

from openai import OpenAI
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.trial_response import TrialResponse

client = OpenAI(api_key=settings.OPENAI_API_KEY)

SYSTEM_PROMPT = """너는 자폐성장애 중·고등학생의 학교생활 대화 연습을 돕는 힌트 생성 도우미다.

너의 역할은 학생의 답장을 채점하는 것이 아니라, 학생이 자신의 답장을 스스로 수정할 수 있도록 짧은 힌트를 제공하는 것이다.

반드시 지켜야 할 규칙:
1. 정답/오답을 말하지 않는다.
2. 점수를 부여하지 않는다.
3. 학생을 대신해 완성된 답장을 작성하지 않는다.
4. 새로운 상황을 만들지 않는다.
5. 상담자, 치료자, 진단자 역할을 하지 않는다.
6. 개인정보를 묻지 않는다.
7. 위험한 조언을 하지 않는다.
8. 힌트는 1~2문장 이내의 쉬운 한국어로만 작성한다.
9. 요청된 hint_level에 맞는 힌트만 제공한다.
10. 3단계 예시 답안은 생성하지 않는다. 예시 답안은 서버 DB에서 제공된다.

힌트 수준:
- 1단계: 방향 힌트. 친구의 감정이나 상황을 생각하게 돕는다.
- 2단계: 구체 힌트. 넣어볼 수 있는 표현 유형을 알려준다.
- 완성 답장 예시는 절대 만들지 않는다.

반드시 JSON 형식으로만 응답한다."""

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "hint_level": {"type": "integer", "enum": [1, 2]},
        "hint_focus": {
            "type": "string",
            "enum": [
                "정서 맥락 파악",
                "감정 인정 표현",
                "상대 중심 대화 반응",
                "도움 요청 반응",
                "관계 회복 반응",
                "기타",
            ],
        },
        "message_to_student": {"type": "string"},
        "contains_full_answer": {"type": "boolean"},
        "contains_scoring": {"type": "boolean"},
        "safety_flag": {
            "type": "string",
            "enum": ["none", "privacy", "self_harm", "violence", "abuse", "inappropriate", "other"],
        },
    },
    "required": [
        "hint_level",
        "hint_focus",
        "message_to_student",
        "contains_full_answer",
        "contains_scoring",
        "safety_flag",
    ],
    "additionalProperties": False,
}

MAX_HINT_LENGTH = 120
PERSONAL_INFO_KEYWORDS = ["이름이 뭐", "몇 살", "나이가", "학교가 어디", "전화번호", "사는 곳", "주소가"]


def _validate_parsed(parsed: dict, item: Item) -> bool:
    if parsed.get("contains_full_answer") is not False:
        return False
    if parsed.get("contains_scoring") is not False:
        return False
    if parsed.get("safety_flag") != "none":
        return False

    message = parsed.get("message_to_student", "")
    if not message or len(message) > MAX_HINT_LENGTH:
        return False
    if item.verified_example and item.verified_example in message:
        return False
    if any(keyword in message for keyword in PERSONAL_INFO_KEYWORDS):
        return False

    return True


def request_hint(db: DbSession, trial: TrialResponse, item: Item, hint_level: int) -> str:
    student_response = trial.first_response if hint_level == 1 else trial.revised_response_1
    fallback_template = item.hint1_template if hint_level == 1 else item.hint2_template

    payload = {
        "session_ref": trial.id,
        "item_id": item.item_id,
        "situation_type": item.situation_type,
        "emotion_tag": item.emotion_tag,
        "item_text": item.item_text,
        "student_response": student_response,
        "hint_level": hint_level,
        "target_response": (item.target_response or "").split(", "),
        "allowed_hint_templates": {
            "hint1": item.hint1_template,
            "hint2": item.hint2_template,
        },
        "forbidden": [
            "정답/오답 판정 금지",
            "점수 부여 금지",
            "완성 답장 제공 금지",
            "새로운 상황 생성 금지",
            "상담자/치료자/진단자 역할 금지",
            "개인정보 질문 금지",
        ],
    }

    raw_content = None
    hint_message = fallback_template
    fallback_used = True
    contains_scoring = False
    contains_full_answer = False
    safety_flag = "none"

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "hint_response",
                    "schema": RESPONSE_JSON_SCHEMA,
                    "strict": True,
                },
            },
        )
        raw_content = response.choices[0].message.content
        parsed = json.loads(raw_content)

        if _validate_parsed(parsed, item):
            hint_message = parsed["message_to_student"]
            fallback_used = False
            contains_scoring = parsed["contains_scoring"]
            contains_full_answer = parsed["contains_full_answer"]
            safety_flag = parsed["safety_flag"]
    except Exception:
        pass

    db.add(
        AiHintLog(
            trial_id=trial.id,
            hint_level=hint_level,
            prompt_payload=json.dumps(payload, ensure_ascii=False),
            model_name=settings.OPENAI_MODEL,
            api_response_raw=raw_content,
            hint_message=hint_message,
            fallback_used=fallback_used,
            contains_scoring=contains_scoring,
            contains_full_answer=contains_full_answer,
            safety_flag=safety_flag,
        )
    )
    db.commit()

    return hint_message
