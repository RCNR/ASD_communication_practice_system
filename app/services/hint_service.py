from __future__ import annotations

import json

from openai import OpenAI
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.models.ai_hint_log import AiHintLog
from app.models.item import Item
from app.models.trial_response import TrialResponse

client = OpenAI(api_key=settings.OPENAI_API_KEY)

SYSTEM_PROMPT = """너는 자폐성장애 중·고등학생의 학교생활 대화 연습을 돕는 채점 도우미다.

너의 역할은 학생의 답장에 아래 두 요소가 있는지를 각각 true/false로 판단하는 것이다. 점수 자체는 시스템이
이 두 값으로부터 계산하므로 너는 점수를 직접 매기지 않는다.

- 인정(acknowledge): 친구가 한 말에 대한 감정적 반응. sentiment가 positive면 축하·기쁨 표현, negative면
  위로·공감 표현이 인정에 해당한다.
- 이어가기(continue): 되묻거나 제안하는 등 대화를 계속 이어가려는 시도.

판단 참고:
- example_score_2는 인정+이어가기가 둘 다 있는 예시, example_score_1_ack는 인정만 있는 예시,
  example_score_1_con은 이어가기만 있는 예시, example_score_0은 둘 다 없는 예시다. 학생 답이 예시와
  똑같지 않아도 같은 전략을 쓰고 있으면 해당 요소가 있다고 본다.
- 판단이 애매하면 관대하게 있다고(true) 본다. 이 프로그램의 목적은 정답을 가려내는 것이 아니라 학생이
  자신감을 갖고 연습하는 것이다.
- 맞춤법이나 띄어쓰기에 경미한 오류가 있어도 의미 전달에 지장이 없으면 감점하지 않는다. 채점은 오직
  내용(인정, 이어가기)만 기준으로 한다.
- acknowledge와 continue가 둘 다 false인 경우(상황과 무관하거나 "몰라"처럼 주제를 벗어난 답 포함)에만
  feedback_message에 전략 힌트를 담는다. 그 외에는 feedback_message를 빈 문자열로 둔다.
- 답장에 욕설이나 비속어가 있으면 safety_flag를 "inappropriate"로 설정한다 (그 외에는 "none").

반드시 지켜야 할 규칙:
1. 학생을 대신해 완성된 답장을 작성하지 않는다. 힌트에 정답 문장 전체는 물론, 정답에만 등장하는 구체적인
   단어·표현도 넣지 않는다.
2. 힌트는 "무엇을 해야 하는지" 전략만 짧게 알려준다. 예: "친구가 말한 일에 대해 더 알고 싶은 점을 물어보세요."
   또는 "친구가 지금 어떤 마음일지 표현해보세요." 처럼, 내용이 아니라 행동 지침 형태로 작성한다.
3. 새로운 상황을 만들지 않는다.
4. 상담자, 치료자, 진단자 역할을 하지 않는다.
5. 개인정보를 묻지 않는다.
6. 위험한 조언을 하지 않는다.
7. 힌트는 1~2문장 이내의 쉬운 한국어로만 작성한다.

반드시 JSON 형식으로만 응답한다."""

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "acknowledge": {"type": "boolean"},
        "continue": {"type": "boolean"},
        "feedback_message": {"type": "string"},
        "contains_full_answer": {"type": "boolean"},
        "safety_flag": {
            "type": "string",
            "enum": ["none", "privacy", "self_harm", "violence", "abuse", "inappropriate", "other"],
        },
    },
    "required": ["acknowledge", "continue", "feedback_message", "contains_full_answer", "safety_flag"],
    "additionalProperties": False,
}

MAX_MESSAGE_LENGTH = 120
PERSONAL_INFO_KEYWORDS = ["이름이 뭐", "몇 살", "나이가", "학교가 어디", "전화번호", "사는 곳", "주소가"]
PROFANITY_MESSAGE = "적합하지 않은 표현입니다. 다른 방식으로 대답해 볼까요?"

CONTENT_SAFETY_SYSTEM_PROMPT = """너는 자폐성장애 학생이 쓴 대화 연습 답장에 안전 문제가 있는지만 판단하는
필터다. 채점이나 힌트 작성은 네 역할이 아니다.

아래 중 하나에 명확히 해당하면 그 항목명을 반환하고, 아니면 "none"을 반환한다. 욕설/비속어는 이 필터의
대상이 아니다 (별도로 0점 처리되므로 여기서는 무시한다).
- self_harm: 자해, 자살에 대한 생각이나 의도 표현
- abuse: 폭행, 학대를 당하고 있다는 고백
- violence: 타인에 대한 폭력적 표현
- sexual: 성적인 표현
- privacy: 실명, 전화번호, 주소, 학교명 등 개인정보 노출

판단이 애매하면 관대하게 "none"으로 본다. 자연스러운 감정 표현이나 이 항목과 무관한 내용은 전부 none이다.
특히 self_harm, abuse는 참여자 본인의 안전과 직결되므로 조금이라도 암시가 있으면 놓치지 말고 표시한다.
반드시 JSON 형식으로만 응답한다."""

CONTENT_SAFETY_SCHEMA = {
    "type": "object",
    "properties": {
        "safety_flag": {
            "type": "string",
            "enum": ["none", "self_harm", "abuse", "violence", "sexual", "privacy"],
        },
    },
    "required": ["safety_flag"],
    "additionalProperties": False,
}

# All flagged categories go through the same rewrite loop (ask the student to
# rewrite, with a retry cap - see student.py's SAFETY_REWRITE_LIMIT). Once the
# cap is exceeded for any category - including self_harm/abuse - the trial
# escalates to the safety-warning stop screen, so a repeated disclosure still
# eventually reaches a human even though it isn't stopped on first mention.
# inappropriate (profanity) is deliberately NOT here: it's scored as a normal
# 0-point response instead (see evaluate_answer), not a rewrite-loop category.
REWRITE_CATEGORIES = ("self_harm", "abuse", "violence", "sexual", "privacy")

CHECK_FAILED = "check_failed"


def check_content_safety(student_response: str) -> str | None:
    """AI-based safety check covering all categories in REWRITE_CATEGORIES.
    Returns the flag name, CHECK_FAILED if the API call/parse failed, or None
    if the text is clean.

    There is no keyword-based backstop for any category anymore (a deliberate
    product decision to move self_harm/abuse detection to the AI too, and to
    have them go through the same rewrite loop as the other categories rather
    than stopping immediately). On failure this returns CHECK_FAILED rather
    than silently passing the text through, so the caller can ask the student
    to resubmit instead of risking a missed self_harm/abuse disclosure."""
    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": CONTENT_SAFETY_SYSTEM_PROMPT},
                {"role": "user", "content": student_response},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "content_safety",
                    "schema": CONTENT_SAFETY_SCHEMA,
                    "strict": True,
                },
            },
        )
        parsed = json.loads(response.choices[0].message.content)
        flag = parsed.get("safety_flag")
        return flag if flag in REWRITE_CATEGORIES else None
    except Exception:
        return CHECK_FAILED


def _derive_score(acknowledge: bool, continue_flag: bool) -> int:
    if acknowledge and continue_flag:
        return 2
    if acknowledge or continue_flag:
        return 1
    return 0


def _derive_missing(acknowledge: bool, continue_flag: bool) -> str | None:
    """Which element is missing for a 1-point response. None for 0 or 2
    points (0-point uses the AI hint instead; 2-point has nothing missing)."""
    if acknowledge and not continue_flag:
        return "이어가기"
    if continue_flag and not acknowledge:
        return "인정"
    return None


def _validate_parsed(parsed: dict, item: Item) -> bool:
    if parsed.get("contains_full_answer") is not False:
        return False

    safety_flag = parsed.get("safety_flag")
    if safety_flag not in ("none", "inappropriate"):
        return False

    acknowledge = parsed.get("acknowledge")
    continue_flag = parsed.get("continue")
    if not isinstance(acknowledge, bool) or not isinstance(continue_flag, bool):
        return False

    # inappropriate (profanity) always scores 0 with a fixed message (set in
    # evaluate_answer), so the AI's own feedback_message doesn't need to pass
    # the strategic-hint validation below.
    if safety_flag == "none" and _derive_score(acknowledge, continue_flag) == 0:
        message = parsed.get("feedback_message", "")
        if not message or len(message) > MAX_MESSAGE_LENGTH:
            return False
        if item.example_score_2 and item.example_score_2 in message:
            return False
        if any(keyword in message for keyword in PERSONAL_INFO_KEYWORDS):
            return False

    return True


def evaluate_answer(
    db: DbSession, trial: TrialResponse, item: Item, hint_level: int, student_response: str
) -> tuple[int, str | None, str | None]:
    """Calls the AI to judge acknowledge/continue for student_response and
    derives a 0/1/2 score from them. Returns (score, feedback_message,
    missing). feedback_message is only meaningful when score is 0. missing
    is "인정"/"이어가기" when score is 1, else None. Logs the call to
    AiHintLog.

    On API/validation failure, defaults to acknowledge=continue=False (score
    0) - fails toward giving the student more help rather than silently
    skipping a check."""
    fallback_template = item.hint_template

    payload = {
        "session_ref": trial.id,
        "item_id": item.item_id,
        "sentiment": item.sentiment,
        "item_text": item.item_text,
        "student_response": student_response,
        "hint_level": hint_level,
        "example_score_2": item.example_score_2,
        "example_score_1_ack": item.example_score_1_ack,
        "example_score_1_con": item.example_score_1_con,
        "example_score_0": item.example_score_0,
        "reference_hint": fallback_template,
        "forbidden": [
            "완성 답장 제공 금지",
            "정답에만 등장하는 단어/표현 제공 금지",
            "새로운 상황 생성 금지",
            "상담자/치료자/진단자 역할 금지",
            "개인정보 질문 금지",
        ],
    }

    raw_content = None
    acknowledge = False
    continue_flag = False
    feedback_message = fallback_template
    fallback_used = True
    contains_full_answer = False
    safety_flag = "none"
    profanity_detected = False

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
            safety_flag = parsed["safety_flag"]
            profanity_detected = safety_flag == "inappropriate"
            fallback_used = False
            contains_full_answer = parsed["contains_full_answer"]

            if profanity_detected:
                # Profanity always scores 0 (counted normally) with a fixed
                # message, regardless of what the AI judged for
                # acknowledge/continue.
                acknowledge = False
                continue_flag = False
                feedback_message = PROFANITY_MESSAGE
            else:
                acknowledge = parsed["acknowledge"]
                continue_flag = parsed["continue"]
                feedback_message = (
                    parsed["feedback_message"] if _derive_score(acknowledge, continue_flag) == 0 else None
                )
    except Exception:
        pass

    score = _derive_score(acknowledge, continue_flag)
    missing = _derive_missing(acknowledge, continue_flag) if score == 1 else None

    db.add(
        AiHintLog(
            trial_id=trial.id,
            hint_level=hint_level,
            prompt_payload=json.dumps(payload, ensure_ascii=False),
            model_name=settings.OPENAI_MODEL,
            api_response_raw=raw_content,
            hint_message=feedback_message,
            score_level=score,
            acknowledge=acknowledge,
            continue_flag=continue_flag,
            fallback_used=fallback_used,
            contains_scoring=True,  # this call's whole purpose is a correctness judgment
            contains_full_answer=contains_full_answer,
            safety_flag=safety_flag,
            profanity_detected=profanity_detected,
        )
    )
    db.commit()

    return score, feedback_message, missing
