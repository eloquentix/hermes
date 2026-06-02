import httpx
from config import settings

_HEADERS = {"Content-Type": "application/json"}


async def grokipedia(topic: str, max_chars: int = 280) -> str:
    """Ask Grok to write a Grokipedia-style encyclopedia summary of a topic."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={**_HEADERS, "Authorization": f"Bearer {settings.xai_api_key}"},
            json={
                "model": "grok-4.3",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Write a sharp, concise Grokipedia-style encyclopedia summary of '{topic}'. "
                            f"Plain text only, no markdown, max {max_chars} characters."
                        ),
                    }
                ],
                "max_tokens": 300,
            },
        )
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
