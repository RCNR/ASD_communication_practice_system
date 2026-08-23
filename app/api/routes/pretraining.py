from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.routes.student import (
    PRACTICE_USE_TYPE_LABEL,
    RESPONSE_TIMER_SECONDS,
    _current_participant,
    _timer_label,
)
from app.core.database import get_db
from app.models.item import Item
from app.services.hint_service import (
    CHECK_FAILED,
    check_content_safety,
    check_profanity,
    check_response_validity,
    evaluate_answer,
    get_suggestion_message,
)
from app.services.item_import_service import PRACTICE_USE_TYPES

router = APIRouter(prefix="/pretraining")
templates = Jinja2Templates(directory="app/templates")

ACTION_PREFIX = "/pretraining"

# Walkthrough order: the single-step practice items first, then the ones that
# walk through the full hint/example flow.
USE_TYPE_RANK = {"practice_assessment": 0, "practice_intervention": 1}


def _pretraining_items(db: Session) -> list[Item]:
    items = (
        db.query(Item)
        .filter(Item.use_type.in_(PRACTICE_USE_TYPES), Item.status == "approved")
        .order_by(Item.set_no, Item.set_order)
        .all()
    )
    return sorted(items, key=lambda item: USE_TYPE_RANK.get(item.use_type, len(USE_TYPE_RANK)))


def _init_state() -> dict:
    # Deliberately does NOT store the item list/ids here: this dict is
    # serialized into the browser's session cookie, which has a ~4KB size
    # limit. With enough pretraining items, embedding the full id list blew
    # past that limit, the browser silently dropped the cookie, and the
    # participant got stuck in an infinite /pretraining <-> /pretraining/item
    # redirect loop. The ordered item list is cheap to re-query from the DB
    # on every request instead (see _pretraining_items), so only the current
    # index and small per-item fields need to live in the cookie.
    return {
        "index": 0,
        "stage": "first",
        "first_response": None,
        "revised_response_1": None,
        "suggestion_message": None,
        "feedback_message": None,
    }


def _advance_to_next_item(state: dict) -> None:
    state["index"] += 1
    state["stage"] = "first"
    state["first_response"] = None
    state["revised_response_1"] = None
    state["suggestion_message"] = None
    state["feedback_message"] = None


def _content_safety_redirect(response_text: str) -> str | None:
    """Ephemeral counterpart to student.py's _content_safety_redirect: no
    state to mutate since the check just gates whether the caller proceeds.
    Returns a redirect path if the submission should be rejected and
    resubmitted, or None if the text is clean."""
    content_flag = check_content_safety(response_text)

    if content_flag == CHECK_FAILED:
        return f"{ACTION_PREFIX}/item?retry_notice=1"

    if content_flag:
        return f"{ACTION_PREFIX}/item?rewrite_notice=1"

    return None


def _current_item(db: Session, state: dict) -> Item | None:
    items = _pretraining_items(db)
    if state["index"] >= len(items):
        return None
    return items[state["index"]]


@router.get("")
def pretraining_start(request: Request, db: Session = Depends(get_db)):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/home", status_code=303)

    if not _pretraining_items(db):
        return templates.TemplateResponse(request, "pretraining_empty.html")

    request.session["pretraining"] = _init_state()
    return RedirectResponse(url=f"{ACTION_PREFIX}/item", status_code=303)


@router.get("/item")
def pretraining_item(request: Request, db: Session = Depends(get_db)):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/home", status_code=303)

    state = request.session.get("pretraining")
    if state is None:
        return RedirectResponse(url=ACTION_PREFIX, status_code=303)

    items = _pretraining_items(db)
    if state["index"] >= len(items):
        if not participant.pretraining_completed:
            participant.pretraining_completed = True
            db.commit()
        return templates.TemplateResponse(request, "pretraining_complete.html")

    item = items[state["index"]]
    session_label = {
        "phase_label": "사전교육",
        "session_number": state["index"] + 1,
        "session_target": len(items),
    }
    base_context = {
        "item": item,
        "trial_id": state["index"],
        "progress_current": state["index"] + 1,
        "progress_total": len(items),
        "action_prefix": ACTION_PREFIX,
        "item_phase_label": PRACTICE_USE_TYPE_LABEL.get(item.use_type),
        "rewrite_notice": request.query_params.get("rewrite_notice") == "1",
        "retry_notice": request.query_params.get("retry_notice") == "1",
        "invalid_notice": request.query_params.get("invalid_notice") == "1",
        "timer_seconds": RESPONSE_TIMER_SECONDS,
        "timer_label": _timer_label(RESPONSE_TIMER_SECONDS),
        **session_label,
    }

    if item.use_type == "practice_assessment":
        # Single-step, same as a baseline/maintenance session item: answer once
        # and move on. The one thing that pauses the flow is the spelling
        # notice, which is shown attached to the item it refers to.
        return templates.TemplateResponse(
            request,
            "session_item.html",
            {
                **base_context,
                "spelling_notice": state["stage"] == "spelling_notice",
                "awaiting_advance": state["stage"] == "spelling_notice",
                "advance_method": "post",
                "advance_action": f"{ACTION_PREFIX}/finalize",
            },
        )

    stage = state["stage"]
    if stage == "first":
        return templates.TemplateResponse(request, "intervention_item.html", {**base_context, "stage": "first"})

    if stage == "adequate":
        return templates.TemplateResponse(
            request,
            "intervention_item.html",
            {
                **base_context,
                "stage": "adequate",
                "first_response": state["first_response"],
                "revised_response_1": state["revised_response_1"],
                "suggestion_message": state["suggestion_message"],
            },
        )

    if stage == "hint_wait_revision":
        return templates.TemplateResponse(
            request,
            "intervention_item.html",
            {
                **base_context,
                "stage": "hint_wait_revision",
                "first_response": state["first_response"],
                "feedback_message": state["feedback_message"],
                "suggestion_message": state["suggestion_message"],
            },
        )

    return templates.TemplateResponse(
        request,
        "intervention_item.html",
        {
            **base_context,
            "stage": "example_wait_final_revision",
            "first_response": state["first_response"],
            "revised_response_1": state["revised_response_1"],
            "example_text": item.example_score_2,
        },
    )


@router.post("/first-response")
def pretraining_first_response(
    request: Request,
    response_text: str = Form(""),
    timed_out: str = Form("0"),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/home", status_code=303)

    state = request.session.get("pretraining")
    if state is None:
        return RedirectResponse(url=ACTION_PREFIX, status_code=303)

    item = _current_item(db, state)
    if item is not None and item.use_type == "practice_intervention" and state["stage"] == "first":
        if timed_out != "1":
            if not check_response_validity(response_text):
                return RedirectResponse(url=f"{ACTION_PREFIX}/item?invalid_notice=1", status_code=303)

            redirect_url = _content_safety_redirect(response_text)
            if redirect_url:
                request.session["pretraining"] = state
                return RedirectResponse(url=redirect_url, status_code=303)

            if check_profanity(response_text):
                return RedirectResponse(url=f"{ACTION_PREFIX}/item?rewrite_notice=1", status_code=303)

        state["first_response"] = response_text
        score, feedback_message, missing, _ = evaluate_answer(
            db, None, item, hint_level=1, student_response=response_text, with_hint=True
        )
        if score == 2:
            state["stage"] = "adequate"
            state["suggestion_message"] = get_suggestion_message(missing, item.sentiment)
        else:
            state["stage"] = "hint_wait_revision"
            state["feedback_message"] = feedback_message
            # 1점이면 잘한 요소를 짚어주는 고정 제안이 힌트 위에 함께 나온다.
            # 0점이면 missing이 None이라 제안 없이 힌트만 나온다.
            state["suggestion_message"] = get_suggestion_message(missing, item.sentiment)

    request.session["pretraining"] = state
    return RedirectResponse(url=f"{ACTION_PREFIX}/item", status_code=303)


@router.post("/revise")
def pretraining_revise(
    request: Request,
    hint_level: int = Form(...),
    response_text: str = Form(""),
    timed_out: str = Form("0"),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/home", status_code=303)

    state = request.session.get("pretraining")
    if state is None:
        return RedirectResponse(url=ACTION_PREFIX, status_code=303)

    item = _current_item(db, state)
    if item is None or item.use_type != "practice_intervention":
        return RedirectResponse(url=f"{ACTION_PREFIX}/item", status_code=303)

    if timed_out != "1":
        if not check_response_validity(response_text):
            return RedirectResponse(url=f"{ACTION_PREFIX}/item?invalid_notice=1", status_code=303)

        redirect_url = _content_safety_redirect(response_text)
        if redirect_url:
            request.session["pretraining"] = state
            return RedirectResponse(url=redirect_url, status_code=303)

        if check_profanity(response_text):
            request.session["pretraining"] = state
            return RedirectResponse(url=f"{ACTION_PREFIX}/item?rewrite_notice=1", status_code=303)

    if hint_level == 1:
        state["revised_response_1"] = response_text
        score, _, missing, _ = evaluate_answer(db, None, item, hint_level=2, student_response=response_text)
        if score < 2:
            state["stage"] = "example_wait_final_revision"
        else:
            state["stage"] = "adequate"
            state["suggestion_message"] = get_suggestion_message(missing, item.sentiment)
    elif hint_level == 2:
        _advance_to_next_item(state)

    request.session["pretraining"] = state
    return RedirectResponse(url=f"{ACTION_PREFIX}/item", status_code=303)


@router.post("/finalize")
def pretraining_finalize(request: Request, db: Session = Depends(get_db)):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/home", status_code=303)

    state = request.session.get("pretraining")
    if state is None:
        return RedirectResponse(url=ACTION_PREFIX, status_code=303)

    # "adequate" is the intervention flow's pass state; "spelling_notice" is
    # the practice_assessment flow's one pause. Both advance on the same button.
    if state["index"] < len(_pretraining_items(db)) and state["stage"] in ("adequate", "spelling_notice"):
        _advance_to_next_item(state)

    request.session["pretraining"] = state
    return RedirectResponse(url=f"{ACTION_PREFIX}/item", status_code=303)


@router.post("/respond")
def pretraining_respond(
    request: Request,
    response_text: str = Form(""),
    timed_out: str = Form("0"),
    db: Session = Depends(get_db),
):
    participant = _current_participant(request, db)
    if not participant:
        return RedirectResponse(url="/home", status_code=303)

    state = request.session.get("pretraining")
    if state is None:
        return RedirectResponse(url=ACTION_PREFIX, status_code=303)

    item = _current_item(db, state)
    if item is not None and item.use_type == "practice_assessment" and state["stage"] == "first":
        if timed_out != "1":
            if not check_response_validity(response_text):
                return RedirectResponse(url=f"{ACTION_PREFIX}/item?invalid_notice=1", status_code=303)

            redirect_url = _content_safety_redirect(response_text)
            if redirect_url:
                return RedirectResponse(url=redirect_url, status_code=303)

            if check_profanity(response_text):
                return RedirectResponse(url=f"{ACTION_PREFIX}/item?rewrite_notice=1", status_code=303)

        # Scored silently, exactly like a baseline/maintenance session item:
        # the participant sees no feedback, only the spelling notice.
        score, _, _, spelling_issue = evaluate_answer(
            db, None, item, hint_level=1, student_response=response_text
        )
        if score in (1, 2) and spelling_issue:
            state["stage"] = "spelling_notice"
        else:
            _advance_to_next_item(state)

    request.session["pretraining"] = state
    return RedirectResponse(url=f"{ACTION_PREFIX}/item", status_code=303)
