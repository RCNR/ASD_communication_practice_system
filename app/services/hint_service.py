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
이 두 값으로부터 계산하므로 너는 점수를 직접 매기지 않는다. 힌트를 쓰는 것도 네 역할이 아니다.

- 인정(acknowledge): 친구가 드러낸 정서를 알아주는 말. sentiment가 positive면 축하·기쁨 표현, negative면
  위로·공감 표현이 인정에 해당한다.
- 이어가기(continue): 대화의 초점을 친구에게 유지한 채 덧붙이는 말. 관련된 질문, 위로·지지, 도움 제안,
  자기 경험 나누기가 모두 여기에 해당한다.

판단 참고:
- 판단이 애매하면 관대하게 있다고(true) 본다. 이 프로그램의 목적은 정답을 가려내는 것이 아니라 학생이
  자신감을 갖고 연습하는 것이다.
- 짧은 반응이라도 두 요소가 담겨 있으면 있다고 본다.
- 맞춤법이나 띄어쓰기에 오류가 있어도 감점하지 않는다. 채점은 오직 내용(인정, 이어가기)만 기준으로 한다.
- 답장에 욕설이나 비속어가 있으면 safety_flag를 "inappropriate"로 설정한다 (그 외에는 "none").
- 맞춤법이나 띄어쓰기에 오류가 있으면 spelling_issue를 true로 설정한다 (없으면 false). 이 값은 점수에
  전혀 영향을 주지 않는다 - 위에서 말했듯 맞춤법/띄어쓰기 오류는 감점 사유가 아니며, 오직 학생에게 다음
  문항에서 맞춤법에 신경 써 보라는 별도 안내를 보여주기 위한 값이다.

반드시 JSON 형식으로만 응답한다."""

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "acknowledge": {"type": "boolean"},
        "continue": {"type": "boolean"},
        "safety_flag": {
            "type": "string",
            "enum": ["none", "privacy", "self_harm", "violence", "abuse", "inappropriate", "other"],
        },
        "spelling_issue": {"type": "boolean"},
    },
    "required": ["acknowledge", "continue", "safety_flag", "spelling_issue"],
    "additionalProperties": False,
}

HINT_SYSTEM_PROMPT = """너는 자폐성장애 중·고등학생의 학교생활 대화 연습을 돕는 힌트 도우미다.

학생이 친구의 메시지에 답장을 썼지만 필요한 요소가 빠졌다. 학생이 답장을 다시 써 볼 수 있도록
힌트 한 개를 작성하는 것이 네 역할이다. 채점은 네 역할이 아니다.

- 인정: 친구가 드러낸 정서를 알아주는 말
- 이어가기: 대화의 초점을 친구에게 유지한 채 덧붙이는 말

입력의 missing이 이번에 빠진 요소다. missing에 있는 요소에 대해서만 힌트를 쓴다.
- missing이 두 개면 둘 다 안내한다.
- missing이 하나면 나머지 하나는 학생이 이미 잘 해낸 것이다. 이미 잘한 요소를 다시 요구하거나 지적하지
  않는다. 빠진 요소 하나만 안내한다.

입력으로 주어지는 hint_ack는 이 문항에서 알아줘야 할 정서, hint_con은 이어갈 만한 방향이다. missing에
해당하는 재료만 주어지므로, 그것을 학생이 이해할 수 있는 말로 풀어서 힌트를 만든다.

반드시 지켜야 할 규칙:
1. 학생을 대신해 완성된 답장을 작성하지 않는다. 정답 문장 전체는 물론, 답장에 그대로 쓸 수 있는 구체적인
   문구도 넣지 않는다.
2. "무엇을 해야 하는지" 전략만 알려준다. 예: "친구가 지금 어떤 마음일지 알아주는 말을 먼저 쓰고, 그 일에
   대해 궁금한 점을 물어보세요." 처럼 내용이 아니라 행동 지침 형태로 작성한다.
3. 새로운 상황을 만들지 않는다.
4. 상담자, 치료자, 진단자 역할을 하지 않는다.
5. 개인정보를 묻지 않는다.
6. 위험한 조언을 하지 않는다.
7. 1~2문장 이내의 쉬운 한국어로만 작성한다.

반드시 JSON 형식으로만 응답한다."""

HINT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "hint": {"type": "string"},
        "contains_full_answer": {"type": "boolean"},
    },
    "required": ["hint", "contains_full_answer"],
    "additionalProperties": False,
}

MAX_MESSAGE_LENGTH = 120
PERSONAL_INFO_KEYWORDS = ["이름이 뭐", "몇 살", "나이가", "학교가 어디", "전화번호", "사는 곳", "주소가"]
PROFANITY_MESSAGE = "적합하지 않은 표현입니다. 다른 방식으로 대답해 볼까요?"

# 1점(둘 중 하나만 충족)일 때 보여주는 고정 제안 메시지. AI가 쓰지 않는다.
# 촉구가 아니라 안내라서, 출력만 하고 그 문항에서 답을 다시 받지는 않는다.
SUGGESTION_MESSAGES = {
    ("이어가기", "negative"): "친구 마음을 잘 알아줬어요. 지금 답도 좋지만, 다음에는 힘이 되는 말을 한마디 더 붙여 볼까요?",
    ("이어가기", "positive"): "친구 마음을 잘 알아줬어요. 지금 답도 좋지만, 다음에는 궁금한 걸 하나 물어볼까요?",
    ("인정", "negative"): "대화를 이어가는 말을 잘 썼어요. 지금 답도 좋지만, 다음에는 친구가 어떤 마음일지 알아주는 말을 먼저 넣어 볼까요?",
    ("인정", "positive"): "대화를 이어가는 말을 잘 썼어요. 지금 답도 좋지만, 다음에는 같이 기뻐해 주는 말을 먼저 넣어 볼까요?",
}


def get_suggestion_message(missing: str | None, sentiment: str | None) -> str | None:
    """The fixed 1-point suggestion for this (missing element, sentiment)
    pair. None for 0/2-point responses, which have nothing to suggest."""
    if missing is None:
        return None
    return SUGGESTION_MESSAGES.get((missing, sentiment or "positive"))

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


def _validate_parsed(parsed: dict) -> bool:
    """Sanity-checks the scoring call's response. Only the two booleans plus
    the two informational flags matter here - hint text is validated
    separately in _validate_hint."""
    if parsed.get("safety_flag") not in ("none", "inappropriate"):
        return False

    if not isinstance(parsed.get("acknowledge"), bool) or not isinstance(parsed.get("continue"), bool):
        return False

    return isinstance(parsed.get("spelling_issue"), bool)


def _validate_hint(parsed: dict, item: Item) -> bool:
    """Rejects a generated hint that leaks the answer, runs long, or asks for
    personal information. A rejected hint falls back to item.hint_fallback."""
    if parsed.get("contains_full_answer") is not False:
        return False

    hint = parsed.get("hint", "")
    if not hint or len(hint) > MAX_MESSAGE_LENGTH:
        return False
    if item.example_score_2 and item.example_score_2 in hint:
        return False
    return not any(keyword in hint for keyword in PERSONAL_INFO_KEYWORDS)


def generate_hint(
    item: Item, student_response: str, missing: list[str]
) -> tuple[str | None, bool]:
    """Hint for a response that scored below 2. missing is which elements the
    response lacked - ["인정", "이어가기"] for 0 points, one of them for 1.
    Returns (hint, fallback_used).

    Only the missing elements' material is sent: for a 1-point response,
    passing the element the student already got right invites a hint that
    re-demands work they already did.

    Deliberately a separate call from evaluate_answer: the scoring prompt must
    not see hint_ack/hint_con (they'd bias the judgment toward one "correct"
    strategy) and the hint prompt must not see the example_* columns (the hint
    would then leak the step-3 answer). Neither prompt gets both.

    Falls back to item.hint_fallback on API/validation failure so the student
    still sees something in the hint slot."""
    payload = {
        "item_text": item.item_text,
        "student_response": student_response,
        "sentiment": item.sentiment,
        "missing": missing,
    }
    if "인정" in missing:
        payload["hint_ack"] = item.hint_ack
    if "이어가기" in missing:
        payload["hint_con"] = item.hint_con

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": HINT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "hint_response", "schema": HINT_JSON_SCHEMA, "strict": True},
            },
        )
        parsed = json.loads(response.choices[0].message.content)
        if _validate_hint(parsed, item):
            return parsed["hint"], False
    except Exception:
        pass

    return item.hint_fallback, True


def evaluate_answer(
    db: DbSession,
    trial: TrialResponse | None,
    item: Item,
    hint_level: int,
    student_response: str,
    with_hint: bool = False,
) -> tuple[int, str | None, str | None, bool]:
    """Calls the AI to judge acknowledge/continue for student_response and
    derives a 0/1/2 score from them. Returns (score, hint_message, missing,
    spelling_issue). missing is "인정"/"이어가기" when score is 1, else None.
    spelling_issue is whether the AI flagged a spelling/spacing error - it
    never affects score. Logs the call to AiHintLog, unless trial is None
    (used for ephemeral, non-persisted practice sessions - e.g. pretraining -
    where there is no real trial row to attach the log to).

    hint_message is only produced when with_hint is True and the score is
    below 2, via a second AI call (generate_hint). A 1-point response gets a
    hint about the one element it missed; a 0-point one gets both. Callers
    that never show a hint - baseline/maintenance, and the intervention step
    that already had its one hint - leave with_hint False so no hint call is
    made at all.

    The scoring prompt deliberately receives neither the example_* columns nor
    the hint_* columns: examples would turn the judgment into "is this similar
    to the sample answer", which misses correct answers worded differently.

    On API/validation failure, defaults to acknowledge=continue=False (score
    0) - fails toward giving the student more help rather than silently
    skipping a check."""
    payload = {
        "session_ref": trial.id if trial is not None else "pretraining",
        "item_id": item.item_id,
        "sentiment": item.sentiment,
        "item_text": item.item_text,
        "student_response": student_response,
        "hint_level": hint_level,
    }

    raw_content = None
    acknowledge = False
    continue_flag = False
    fallback_used = True
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

        if _validate_parsed(parsed):
            safety_flag = parsed["safety_flag"]
            profanity_detected = safety_flag == "inappropriate"
            fallback_used = False
            spelling_issue = parsed["spelling_issue"]

            if not profanity_detected:
                # Profanity always scores 0 (counted normally) with a fixed
                # message, regardless of what the AI judged for
                # acknowledge/continue - so its booleans are left at False.
                acknowledge = parsed["acknowledge"]
                continue_flag = parsed["continue"]
    except Exception:
        pass

    score = _derive_score(acknowledge, continue_flag)
    missing = _derive_missing(acknowledge, continue_flag) if score == 1 else None

    hint_message = None
    hint_fallback_used = False
    if profanity_detected:
        hint_message = PROFANITY_MESSAGE
    elif with_hint and score < 2:
        hint_message, hint_fallback_used = generate_hint(
            item, student_response, [missing] if missing else ["인정", "이어가기"]
        )

    if trial is not None:
        db.add(
            AiHintLog(
                trial_id=trial.id,
                hint_level=hint_level,
                prompt_payload=json.dumps(payload, ensure_ascii=False),
                model_name=settings.OPENAI_MODEL,
                api_response_raw=raw_content,
                hint_message=hint_message,
                score_level=score,
                acknowledge=acknowledge,
                continue_flag=continue_flag,
                fallback_used=fallback_used or hint_fallback_used,
                contains_scoring=True,  # this call's whole purpose is a correctness judgment
                contains_full_answer=False,
                safety_flag=safety_flag,
                profanity_detected=profanity_detected,
                spelling_issue=spelling_issue,
            )
        )
        db.commit()

    return score, hint_message, missing, spelling_issue
