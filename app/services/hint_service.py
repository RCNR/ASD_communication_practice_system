from __future__ import annotations

import json

from openai import OpenAI
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.trial_response import TrialResponse

client = OpenAI(api_key=settings.OPENAI_API_KEY)

SYSTEM_PROMPT = """너는 자폐성장애 중·고등학생의 학교생활 대화 연습을 돕는 평가 도우미다.

너의 역할은 학생의 답장이 주어진 상황과 목표 반응에 잘 맞는지 판단하고, 부족하다면 학생이 스스로 수정할 수 있도록 짧은 힌트(또는 코멘트)를 제공하는 것이다.

반드시 지켜야 할 규칙:
1. 학생을 대신해 완성된 답장을 작성하지 않는다 (힌트에 정답 문장 전체를 포함하지 않는다).
2. 새로운 상황을 만들지 않는다.
3. 상담자, 치료자, 진단자 역할을 하지 않는다.
4. 개인정보를 묻지 않는다.
5. 위험한 조언을 하지 않는다.
6. 코멘트는 1~2문장 이내의 쉬운 한국어로만 작성한다.

판단 기준 (관대하게 판단할 것):
- 이 프로그램의 목적은 정답을 가려내는 것이 아니라 학생이 자신감을 갖고 연습하는 것이다. 완벽한 답이 아니어도 괜찮다.
- 학생의 답장에서 상대방의 감정을 조금이라도 고려하거나 공감하려는 시도가 보이면 is_adequate를 true로 판단한다.
- target_response에 나열된 요소를 전부 만족할 필요는 없다. 그중 하나라도 의미 있게 담겨 있으면 충분하다.
- "몰라", "음", 상황과 무관한 답, 상대방 감정을 전혀 고려하지 않은 답일 때만 is_adequate를 false로 판단한다.
- 판단이 애매하면 관대하게 true로 판단한다.
- is_adequate가 false일 때만 feedback_message에 방향을 알려주는 짧은 코멘트를 담는다 (완성 답장은 절대 쓰지 않는다).
- is_adequate가 true이면 feedback_message는 빈 문자열로 둔다.

반드시 JSON 형식으로만 응답한다."""

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "is_adequate": {"type": "boolean"},
        "feedback_message": {"type": "string"},
        "contains_full_answer": {"type": "boolean"},
        "safety_flag": {
            "type": "string",
            "enum": ["none", "privacy", "self_harm", "violence", "abuse", "inappropriate", "other"],
        },
    },
    "required": ["is_adequate", "feedback_message", "contains_full_answer", "safety_flag"],
    "additionalProperties": False,
}

MAX_MESSAGE_LENGTH = 120
PERSONAL_INFO_KEYWORDS = ["이름이 뭐", "몇 살", "나이가", "학교가 어디", "전화번호", "사는 곳", "주소가"]


def _validate_parsed(parsed: dict, item: Item) -> bool:
    if parsed.get("contains_full_answer") is not False:
        return False
    if parsed.get("safety_flag") != "none":
        return False

    if parsed.get("is_adequate") is False:
        message = parsed.get("feedback_message", "")
        if not message or len(message) > MAX_MESSAGE_LENGTH:
            return False
        if item.verified_example and item.verified_example in message:
            return False
        if any(keyword in message for keyword in PERSONAL_INFO_KEYWORDS):
            return False

    return True


def evaluate_answer(
    db: DbSession, trial: TrialResponse, item: Item, hint_level: int, student_response: str
) -> tuple[bool, str | None]:
    """Calls the AI to judge whether student_response adequately addresses the
    item. Returns (is_adequate, feedback_message). feedback_message is only
    meaningful when is_adequate is False. Logs the call to AiHintLog.

    On API/validation failure, defaults to is_adequate=False (fails toward
    giving the student more help rather than silently skipping a check)."""
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
        "reference_hint": fallback_template,
        "forbidden": [
            "완성 답장 제공 금지",
            "새로운 상황 생성 금지",
            "상담자/치료자/진단자 역할 금지",
            "개인정보 질문 금지",
        ],
    }

    raw_content = None
    is_adequate = False
    feedback_message = fallback_template
    fallback_used = True
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
                    "name": "evaluation_response",
                    "schema": RESPONSE_JSON_SCHEMA,
                    "strict": True,
                },
            },
        )
        raw_content = response.choices[0].message.content
        parsed = json.loads(raw_content)

        if _validate_parsed(parsed, item):
            is_adequate = parsed["is_adequate"]
            feedback_message = parsed["feedback_message"] if not is_adequate else None
            fallback_used = False
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
            hint_message=feedback_message,
            is_adequate=is_adequate,
            fallback_used=fallback_used,
            contains_scoring=True,  # this call's whole purpose is a correctness judgment
            contains_full_answer=contains_full_answer,
            safety_flag=safety_flag,
        )
    )
    db.commit()

    return is_adequate, feedback_message
