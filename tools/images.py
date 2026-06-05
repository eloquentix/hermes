import logging
import httpx
from config import settings

logger = logging.getLogger(__name__)

_CSE_URL = "https://www.googleapis.com/customsearch/v1"
_UA = "Mozilla/5.0 (compatible; inflightbot/1.0)"


async def search_image(query: str) -> dict | None:
    """Search Google Images via Custom Search API, download first working result.

    Returns {"type": "photo", "data": bytes, "filename": str, "caption": str} or None.
    """
    if not settings.google_cse_api_key or not settings.google_cse_cx:
        logger.warning("Google CSE not configured — image search unavailable")
        return None

    # Search
    params = {
        "key": settings.google_cse_api_key,
        "cx": settings.google_cse_cx,
        "q": query,
        "searchType": "image",
        "num": 5,
        "safe": "active",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_CSE_URL, params=params)
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as exc:
        logger.warning("Google CSE search failed: %s", exc)
        return None

    if not items:
        logger.info("No image results for '%s'", query)
        return None

    # Try downloading each result until one works
    for item in items:
        url = item.get("link", "")
        if not url:
            continue
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": _UA})
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "image" not in content_type:
                    continue
                if len(resp.content) < 1000:
                    continue
                ext = "jpg"
                for e in ("png", "webp", "gif", "jpeg"):
                    if e in content_type:
                        ext = e
                        break
                logger.info("Image downloaded from %s (%d bytes)", url[:80], len(resp.content))
                return {
                    "type": "photo",
                    "data": resp.content,
                    "filename": f"image.{ext}",
                    "caption": query,
                }
        except Exception as exc:
            logger.debug("Image download failed for %s: %s", url[:60], exc)
            continue

    logger.warning("All image URLs failed for '%s'", query)
    return None
