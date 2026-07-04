from fastapi import APIRouter

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.openai_service import get_chat_reply

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    reply = await get_chat_reply(request.message)
    return ChatResponse(reply=reply)
