import logging
import re
from datetime import date
import httpx
import anthropic
from google import genai
from google.genai import types as gtypes

from config import settings
from tools.fetch import fetch_url, SCHEMA as FETCH_SCHEMA
from tools.pdf import fetch_pdf, fetch_pdf_bytes, SCHEMA as PDF_SCHEMA
from tools.weather import get_weather
from tools.grok import grokipedia
from tools.stocks import get_stock
from tools.images import search_image

logger = logging.getLogger(__name__)

TOOL_DISPATCH = {
    "fetch_url": lambda inp: fetch_url(inp["url"]),
    "fetch_pdf": lambda inp: fetch_pdf(inp["url"]),
}

_FETCH_HINTS = (
    "http://", "https://", ".ro", ".com", ".org", ".net", ".io",
    "hotnews", "g4media", "digi24", "protv", "antena", "realitatea",
    "site", "page", "article", "link", "url",
)


def _needs_fetch(message: str, history: list) -> bool:
    """Route to Claude if current message OR any recent history references specific sites."""
    combined = message.lower() + " ".join(t for _, t in history).lower()
    return any(hint in combined for hint in _FETCH_HINTS)


def _system_prompt() -> str:
    today = date.today().strftime("%B %d, %Y")
    return f"""You are a concise assistant on Telegram.
Today's date is {today}. Use this when interpreting "this year", "today", "recently", etc.
Small screen, limited WiFi. Every character counts.

TOOL RULES:
- PDF requests: use web_search to find the direct PDF URL, then call fetch_pdf. Never summarize from snippets alone.
- Direct .pdf URLs: call fetch_pdf immediately.
- All other questions: use web_search.

OUTPUT RULES:
- Max {settings.max_response_chars} characters. Plain text only. No markdown, no bullets, no asterisks.
- No preamble. Answer directly.
- PDF: 3-5 key points separated by " | ".
- Score: "Barcelona 2 - Real Madrid 1. Final."
- Never apologize or explain your process.
- Image requests are handled separately. Never say you cannot show images."""


# ── Gemini (primary — free, google_search server-side) ───────────────────────

_gemini = genai.Client(api_key=settings.gemini_api_key)
_gemini_models = [m.strip() for m in settings.gemini_models.split(",")]

GEMINI_TOOLS = [gtypes.Tool(google_search=gtypes.GoogleSearch())]


async def _run_gemini(user_message: str, history: list, preferred_model: str = None) -> tuple[str, str]:
    system = _system_prompt() + "\n\nNote: no PDF fetching — for PDF requests summarize from search results as best you can."
    contents = []
    for role, text in history:
        g_role = "model" if role == "assistant" else "user"
        contents.append(gtypes.Content(role=g_role, parts=[gtypes.Part(text=text)]))
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=user_message)]))

    config = gtypes.GenerateContentConfig(
        tools=GEMINI_TOOLS,
        system_instruction=system,
        max_output_tokens=512,
    )
    # If a model is locked for this conversation, try it first
    model_order = _gemini_models
    if preferred_model and preferred_model in _gemini_models:
        model_order = [preferred_model] + [m for m in _gemini_models if m != preferred_model]

    response = None
    used_model = None
    for model in model_order:
        try:
            response = await _gemini.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
            used_model = model
            logger.info("Gemini model=%s", model)
            break
        except Exception as e:
            if any(c in str(e) for c in ("429", "400", "401", "403", "404")):
                logger.warning("Gemini %s on %s", str(e)[:10], model)
                continue
            raise

    if response is None:
        raise Exception("429 All Gemini models exhausted")

    text_parts = [p.text for p in response.candidates[0].content.parts if getattr(p, "text", None)]
    text = " ".join(text_parts).strip()
    if len(text) > settings.max_response_chars:
        text = text[: settings.max_response_chars].rsplit(" ", 1)[0] + "…"
    return text or "No answer found.", used_model


# ── Claude (fallback — paid, web_search + fetch_pdf + fetch_url) ──────────────

_claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

CLAUDE_TOOLS = [
    {"type": "web_search_20250305", "name": "web_search"},
    FETCH_SCHEMA,
    PDF_SCHEMA,
]


async def _run_claude(user_message: str, history: list) -> tuple[str, str]:
    system = _system_prompt()
    messages = [{"role": role, "content": text} for role, text in history]
    messages.append({"role": "user", "content": user_message})

    for iteration in range(settings.max_tool_iterations):
        response = await _claude.messages.create(
            model=settings.claude_model,
            max_tokens=512,
            system=system,
            tools=CLAUDE_TOOLS,
            messages=messages,
        )
        logger.info("Claude stop_reason=%s iter=%d", response.stop_reason, iteration)

        if response.stop_reason == "end_turn":
            text = " ".join(b.text for b in response.content if hasattr(b, "text")).strip()
            if len(text) > settings.max_response_chars:
                text = text[: settings.max_response_chars].rsplit(" ", 1)[0] + "…"
            return text or "No answer found.", "claude"

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_fn = TOOL_DISPATCH.get(block.name)
                if tool_fn is None:
                    continue
                try:
                    result = await tool_fn(block.input)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})
                except Exception as exc:
                    logger.warning("Tool %s failed: %s", block.name, exc)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {exc}", "is_error": True})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        break

    return "Could not complete that request.", "claude"


# ── Image helpers ────────────────────────────────────────────────────────────

# Positive: "get me an image of", "show picture of", "image of X"
# Negative: exclude complaints like "didn't get image", "no image", "can't see image"
_IMAGE_POSITIVE = re.compile(
    r"(?:get|find|show|send|fetch|give|need|want|see)\s+(?:me\s+)?(?:an?\s+|the\s+)?(?:image|photo|picture|pic|img)"
    r"|(?:image|photo|picture|pic)\s+(?:of|for)\s+",
    re.IGNORECASE,
)
_IMAGE_NEGATIVE = re.compile(
    r"(?:didn.t|did\s+not|don.t|no|can.t|cannot|not)\s+(?:get|see|show|have|find|receive)\s+(?:an?\s+|the\s+)?(?:image|photo|picture)",
    re.IGNORECASE,
)


def _wants_image(message: str) -> bool:
    if _IMAGE_NEGATIVE.search(message):
        return False
    return bool(_IMAGE_POSITIVE.search(message))


def _extract_image_topic(message: str, history: list) -> str:
    """Extract the actual image topic — handle vague requests like 'image of those'."""
    # Strip the image-request preamble to get the topic
    topic = re.sub(
        r"^(?:ok\.?\s*)?(?:get|find|show|send|fetch|give|need|want|see)\s+(?:me\s+)?(?:an?\s+|the\s+)?(?:image|photo|picture|pic|img)\s*(?:of|for)?\s*",
        "", message, flags=re.IGNORECASE,
    ).strip()

    # If topic is vague/empty ("those", "it", "them", "that", "all"), pull from history
    if not topic or topic.lower() in ("those", "it", "them", "that", "all", "all of them", "all 8", "all eight"):
        # Find last substantive assistant message
        for role, text in reversed(history):
            if role == "assistant" and len(text) > 20:
                # Extract first noun phrase / topic — use first sentence or caption
                first_line = text.split("\n")[0].split(".")[0].strip()
                if first_line:
                    topic = first_line[:100]
                    break
        # Also check user messages for the original topic
        if not topic or topic.lower().startswith(("i ", "ok", "the ")):
            for role, text in reversed(history):
                if role == "user" and not _wants_image(text):
                    topic = text.strip()[:100]
                    break

    return topic or message


_IMAGE_URL_RE = re.compile(r"https?://\S+\.(?:jpg|jpeg|png|gif|webp)", re.IGNORECASE)
_WIKI_UA = "inflightbot/1.0 (hermes; contact@eloquentix.com)"


async def _find_image(topic: str) -> dict | None:
    """Find image: Google CSE (if configured) → Wikipedia → Claude web_search."""
    # Strategy 1: Google CSE (if keys configured)
    att = await search_image(topic)
    if att:
        return att

    # Strategy 2: Wikipedia — reliable for known topics
    raw = topic.strip()
    slugs = list(dict.fromkeys([
        raw.replace(" ", "_"),
        raw.title().replace(" ", "_"),
        raw.lower().replace(" ", "_"),
    ]))
    for wiki_slug in slugs:
        wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_slug}"
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(wiki_url, headers={"User-Agent": _WIKI_UA})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                img_url = data.get("originalimage", {}).get("source") or data.get("thumbnail", {}).get("source")
                if not img_url:
                    continue
                img_resp = await client.get(img_url, headers={"User-Agent": _WIKI_UA})
                if img_resp.status_code == 200 and "image" in img_resp.headers.get("content-type", ""):
                    ext = "jpg"
                    for e in ("png", "webp", "gif", "jpeg"):
                        if e in img_resp.headers.get("content-type", ""):
                            ext = e
                            break
                    logger.info("Image found via Wikipedia for '%s'", topic)
                    return {"type": "photo", "data": img_resp.content, "filename": f"image.{ext}", "caption": topic}
        except Exception as exc:
            logger.debug("Wikipedia image failed: %s", exc)

    # Strategy 3: Claude web_search
    try:
        response = await _claude.messages.create(
            model=settings.claude_model, max_tokens=300,
            system="Find direct image URLs (.jpg/.png/.webp) for the query. Return ONLY URLs, one per line.",
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": f"Find 3 direct image URLs for: {topic}"}],
        )
        all_text = " ".join(b.text for b in response.content if hasattr(b, "text"))
        urls = _IMAGE_URL_RE.findall(all_text)
        urls = [u for u in urls if "/thumb/" not in u] or urls
    except Exception as exc:
        logger.warning("Claude image search failed: %s", exc)
        urls = []

    for url in urls[:5]:
        try:
            ua = _WIKI_UA if "wikimedia" in url or "wikipedia" in url else "Mozilla/5.0"
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": ua})
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "image" not in ct or len(resp.content) < 1000:
                    continue
                ext = "jpg"
                for e in ("png", "webp", "gif", "jpeg"):
                    if e in ct:
                        ext = e
                        break
                logger.info("Image downloaded from %s", url[:80])
                return {"type": "photo", "data": resp.content, "filename": f"image.{ext}", "caption": topic}
        except Exception:
            continue

    logger.warning("All image strategies failed for '%s'", topic)
    return None


# ── Slash command handlers ────────────────────────────────────────────────────

async def cmd_weather(args: str) -> str:
    city = args.strip() or "Bucharest"
    try:
        return await get_weather(city)
    except Exception as exc:
        return f"Weather unavailable: {exc}"


async def cmd_wiki(args: str) -> str:
    if not args.strip():
        return "Usage: /wiki <topic>"
    try:
        return await grokipedia(args.strip(), settings.max_response_chars)
    except Exception as exc:
        return f"Grok unavailable: {exc}"


async def cmd_flight(args: str) -> str:
    if not args.strip():
        return "Usage: /flight <number>  e.g. /flight LH441"
    prompt = f"Current status of flight {args.strip().upper()}. One line: flight number, status, departure airport+time, arrival airport+time, any delay."
    answer, _ = await _run_gemini(prompt, [])
    return answer


async def cmd_news(args: str) -> str:
    topic = args.strip() or "world"
    prompt = f"Top 5 news headlines about {topic} right now. One line each, no numbers, separated by ' | '."
    answer, _ = await _run_claude(prompt, [])
    return answer


async def cmd_pdf(args: str) -> tuple[str, list]:
    if not args.strip():
        return "Usage: /pdf <url or document name>", []
    url = args.strip()
    attachments = []
    # If it's a direct PDF URL, also send the file
    if url.lower().startswith("http") and ".pdf" in url.lower():
        try:
            pdf_bytes, filename = await fetch_pdf_bytes(url)
            if len(pdf_bytes) < 50 * 1024 * 1024:  # under 50MB Telegram limit
                attachments.append({"type": "document", "data": pdf_bytes, "filename": filename})
        except Exception as exc:
            logger.warning("PDF download for attachment failed: %s", exc)
    prompt = f"Fetch and summarize this PDF: {url}"
    answer, _ = await _run_claude(prompt, [])
    return answer, attachments


async def cmd_image(args: str) -> tuple[str, list]:
    if not args.strip():
        return "Usage: /image <what you want>", []
    att = await _find_image(args.strip())
    if att:
        return "", [att]
    return f"Couldn't find an image for: {args.strip()}", []


async def cmd_tr(args: str) -> str:
    parts = args.strip().split(" ", 1)
    if len(parts) < 2:
        return "Usage: /tr <language> <text>  e.g. /tr french Good morning"
    lang, text = parts[0], parts[1]
    prompt = f"Translate to {lang}: {text}. Reply with only the translation."
    answer, _ = await _run_gemini(prompt, [])
    return answer


async def cmd_stocks(args: str) -> str:
    if not args.strip():
        return "Usage: /stocks <ticker>  e.g. /stocks AAPL"
    tickers = args.strip().split()
    results = []
    for ticker in tickers[:4]:  # max 4 at once
        try:
            results.append(get_stock(ticker))
        except Exception as exc:
            results.append(f"{ticker.upper()}: error ({exc})")
    return "\n".join(results)


COMMANDS = {
    "weather": cmd_weather,
    "wiki":    cmd_wiki,
    "flight":  cmd_flight,
    "news":    cmd_news,
    "pdf":     cmd_pdf,
    "image":   cmd_image,
    "tr":      cmd_tr,
    "stocks":  cmd_stocks,
}


# ── Public entry point ────────────────────────────────────────────────────────

async def run_agent(user_message: str, history: list = None, preferred_model: str = None) -> tuple[str, str, list]:
    history = history or []

    # Auto-detect image requests in free-form messages
    if _wants_image(user_message):
        topic = _extract_image_topic(user_message, history)
        logger.info("Image request detected, topic='%s'", topic)
        att = await _find_image(topic)
        attachments = [att] if att else []
        # Still get a text answer too
        try:
            answer, model = await _run_gemini(user_message, history, preferred_model)
        except Exception:
            answer, model = await _run_claude(user_message, history)
        return answer, model, attachments

    if _needs_fetch(user_message, history):
        logger.info("Routing to Claude (fetch needed)")
        text, model = await _run_claude(user_message, history)
        return text, model, []
    try:
        text, model = await _run_gemini(user_message, history, preferred_model)
        return text, model, []
    except Exception as exc:
        logger.warning("Gemini failed (%s), falling back to Claude", str(exc)[:60])
        text, model = await _run_claude(user_message, history)
        return text, model, []
