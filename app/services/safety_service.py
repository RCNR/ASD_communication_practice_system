from __future__ import annotations

# Keyword-based detection so this also works in baseline/maintenance, where the
# GPT API must never be called (brief section 2, 13). This is a coarse first
# pass meant to be paired with manual review, not a precise classifier.
SAFETY_KEYWORDS = {
    "self_harm": ["죽고 싶", "자살", "자해", "죽어버리고 싶", "살기 싫"],
    "violence": ["죽여버리", "때리고 싶", "폭력을 쓰고", "칼로"],
    "abuse": ["맞았어", "학대", "폭행당했", "매를 맞"],
    "privacy": ["전화번호는", "제 주소는", "우리 학교는", "실명은", "제 번호"],
    "inappropriate": ["시발", "존나", "개새끼", "병신", "씨발"],
}


def detect_safety_flag(text: str) -> str | None:
    for flag, keywords in SAFETY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return flag
    return None
