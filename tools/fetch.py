import re
import httpx

SCHEMA = {
    "name": "fetch_url",
    "description": (
        "Fetch the text content of a web page. Use when the user provides a URL "
        "or when a search result's full page is needed. Not for PDFs — use fetch_pdf instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full URL to fetch"}
        },
        "required": ["url"],
    },
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; inflightbot/1.0)"}


async def fetch_url(url: str, max_chars: int = 1500) -> str:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type:
        return "This URL is a PDF. Use the fetch_pdf tool instead."

    text = re.sub(r"<[^>]+>", " ", resp.text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
