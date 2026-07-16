import httpx

async def get_wiki(topic: str) -> str:
    """Fetch Wikipedia summary — used as source for Grokipedia-style output."""
    slug = topic.strip().replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "inflightbot/1.0"})
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
    data = resp.json()
    return data.get("extract", "")[:2000]
