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
- 맞춤법이나 띄어쓰기에 오류가 있으면 spelling_issue를 true로 설정한다 (없으면 false). 이 값은 점수에
  전혀 영향을 주지 않는다 - 위에서 말했듯 맞춤법/띄어쓰기 오류는 감점 사유가 아니며, 오직 학생에게 다음
  문항에서 맞춤법에 신경 써 보라는 별도 안내를 보여주기 위한 값이다.

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
        "spelling_issue": {"type": "boolean"},
    },
    "required": [
        "acknowledge",
        "continue",
        "feedback_message",
        "contains_full_answer",
        "safety_flag",
        "spelling_issue",
    ],
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

# All flagged categories are treated the same way: the student is asked to
# rewrite, with no cap or escalation - a repeated flag just keeps asking for
# another rewrite (see student.py's _content_safety_redirect / pretraining.py's
# counterpart). inappropriate (profanity) is deliberately NOT here: it's
# screened separately by check_profanity before save, not through this filter.
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


VALIDITY_SYSTEM_PROMPT = """너는 학생이 쓴 대화 연습 답장이 "문장이나 단어" 형태로 되어 있는지만 판단하는
필터다. 내용이 적절한지, 상황과 관련 있는지, 욕설이 있는지는 전혀 신경 쓰지 않는다 - 오직 사람이 알아볼 수
있는 문장이나 단어인지만 본다.

아래는 valid=false로 판단한다:
- "ㅇㅇ", "ㅋㅋㅋ", "ㅎㅇ" 같은 자음/모음만 나열되거나 감탄사만 있는 경우
- 의미를 알 수 없는 숫자·기호 나열 (예: "1234", "...", "ㅁㄴㅇㄹ")
- 실제 존재하는 단어가 아닌, 키보드를 무작위로 눌러 나온 듯한 글자 나열 (언어 무관 - 예: "aadsf", "asdkfj", "ㅁㄷㄴㄻㅇ" 같이 한글이든 영어든 뜻이 없으면 동일하게 적용)
- 빈 내용이나 공백만 있는 경우

아래는 설령 부적절하거나 상황과 무관해도 valid=true로 판단한다 (내용 판단은 이 필터의 역할이 아니다):
- 욕설이나 비속어가 섞인 문장
- 실제 단어나 문장이면 상황과 관련 없어도 유효함 (예: "몰라", "배고파", "그냥 그래")

반드시 JSON 형식으로만 응답한다."""

VALIDITY_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
    },
    "required": ["valid"],
    "additionalProperties": False,
}


def check_response_validity(text: str) -> bool:
    """Narrow AI check used only in baseline/maintenance (phases that
    otherwise never call the AI): judges only whether text is a real
    sentence/word, not whether it's appropriate or on-topic - profanity still
    counts as valid here, since content judgment isn't this filter's job.

    Fails open (returns True) on API/parse failure: this is a UX guard
    against blank/gibberish input, not a safety gate, so an API hiccup
    shouldn't block baseline/maintenance progress."""
    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": VALIDITY_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "response_validity",
                    "schema": VALIDITY_SCHEMA,
                    "strict": True,
                },
            },
        )
        parsed = json.loads(response.choices[0].message.content)
        valid = parsed.get("valid")
        return valid if isinstance(valid, bool) else True
    except Exception:
        return True


PROFANITY_SYSTEM_PROMPT = """너는 학생이 쓴 대화 연습 답장에 욕설이나 비속어가 포함되어 있는지만 판단하는
필터다. 상황과 관련 있는지, 전략이 적절한지는 전혀 신경 쓰지 않는다 - 오직 욕설/비속어 포함 여부만 본다.

판단이 애매하면 관대하게 없다고(false) 본다.
반드시 JSON 형식으로만 응답한다."""

PROFANITY_SCHEMA = {
    "type": "object",
    "properties": {
        "contains_profanity": {"type": "boolean"},
    },
    "required": ["contains_profanity"],
    "additionalProperties": False,
}


def check_profanity(text: str) -> bool:
    """Narrow AI check used only for the intervention hint loop's final
    revision (hint_level 2), which otherwise skips evaluate_answer entirely
    (see session_revise) and so never runs the acknowledge/continue prompt
    that normally catches profanity via safety_flag == "inappropriate".
    Without this, a student could submit profanity at that last step and
    have it saved as final_response with zero screening.

    Fails open (returns False) on API/parse failure: this is a narrow UX
    guard on top of the already-passed content-safety check, not the
    primary safety gate, so an API hiccup shouldn't block finishing the
    trial."""
    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": PROFANITY_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "profanity_check",
                    "schema": PROFANITY_SCHEMA,
                    "strict": True,
                },
            },
        )
        parsed = json.loads(response.choices[0].message.content)
        contains_profanity = parsed.get("contains_profanity")
        return contains_profanity if isinstance(contains_profanity, bool) else False
    except Exception:
        return False


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

    if not isinstance(parsed.get("spelling_issue"), bool):
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
    db: DbSession, trial: TrialResponse | None, item: Item, hint_level: int, student_response: str
) -> tuple[int, str | None, str | None, bool]:
    """Calls the AI to judge acknowledge/continue for student_response and
    derives a 0/1/2 score from them. Returns (score, feedback_message,
    missing, spelling_issue). feedback_message is only meaningful when score
    is 0. missing is "인정"/"이어가기" when score is 1, else None.
    spelling_issue is whether the AI flagged a spelling/spacing error - it
    never affects score. Logs the call to AiHintLog, unless trial is None
    (used for ephemeral, non-persisted practice sessions - e.g. pretraining -
    where there is no real trial row to attach the log to).

    On API/validation failure, defaults to acknowledge=continue=False (score
    0) - fails toward giving the student more help rather than silently
    skipping a check."""
    fallback_template = item.hint_template

    payload = {
        "session_ref": trial.id if trial is not None else "pretraining",
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
    spelling_issue = False

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
            spelling_issue = parsed["spelling_issue"]

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

    if trial is not None:
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
                spelling_issue=spelling_issue,
            )
        )
        db.commit()

    return score, feedback_message, missing, spelling_issue
