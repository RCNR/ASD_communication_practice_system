from openai import AsyncOpenAI

from app.core.config import settings

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def get_chat_reply(message: str) -> str:
    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[{"role": "user", "content": message}],
    )
    return response.choices[0].message.content
