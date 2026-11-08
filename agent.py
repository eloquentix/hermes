import asyncio
import logging
import re
from datetime import date, datetime
import httpx
import anthropic
from google import genai
from google.genai import types as gtypes

from config import settings

# ── Daily cost tracking ─────────────────────────────────────────────────────

_cost_today: float = 0.0
_cost_date: str = ""
_cost_calls: int = 0
_cost_by_model: dict[str, float] = {}

# USD per million tokens (input, output), matched by model id prefix
_PRICING = (
    ("claude-opus", (5.0, 25.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
    ("claude", (3.0, 15.0)),
    ("grok", (3.0, 15.0)),
)
WEB_SEARCH_USD = 0.01  # Claude web_search: $10 per 1,000 searches


def _track_cost(input_tokens: int, output_tokens: int, model: str, cost_usd: float = None, searches: int = 0) -> None:
    """Track spend. cost_usd overrides token math when the provider reports exact cost."""
    global _cost_today, _cost_date, _cost_calls
    today = date.today().isoformat()
    if _cost_date != today:
        _cost_today = 0.0
        _cost_calls = 0
        _cost_by_model.clear()
        _cost_date = today
    if cost_usd is None:
        price_in, price_out = 0.0, 0.0
        for prefix, (p_in, p_out) in _PRICING:
            if model.startswith(prefix):
                price_in, price_out = p_in, p_out
                break
        cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    else:
        cost = cost_usd
    cost += searches * WEB_SEARCH_USD
    _cost_today += cost
    _cost_by_model[model] = _cost_by_model.get(model, 0.0) + cost
    _cost_calls += 1


def _track_claude(response, model: str) -> None:
    server_tools = getattr(response.usage, "server_tool_use", None)
    searches = getattr(server_tools, "web_search_requests", 0) or 0
    _track_cost(response.usage.input_tokens, response.usage.output_tokens, model, searches=searches)


def get_daily_cost() -> str:
    today = date.today().isoformat()
    if _cost_date != today or not _cost_calls:
        return f"Today ({today}): $0.00 — 0 paid API calls. Gemini calls are free."
    lines = [f"Today ({_cost_date}): ${_cost_today:.4f} — {_cost_calls} paid API calls"]
    for model, cost in sorted(_cost_by_model.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {model}: ${cost:.4f}")
    lines.append("Gemini calls are free.")
    return "\n".join(lines)
from tools.fetch import fetch_url, SCHEMA as FETCH_SCHEMA
from tools.pdf import fetch_pdf, fetch_pdf_bytes, SCHEMA as PDF_SCHEMA
from tools.weather import get_weather
from tools.grok import grokipedia
from tools.stocks import get_stock
from tools.images import search_image
from tools.flight import flight_status
from tools.wiki import get_wiki
from tools.netguard import public_client

logger = logging.getLogger(__name__)

TOOL_DISPATCH = {
    "fetch_url": lambda inp: fetch_url(inp["url"]),
    "fetch_pdf": lambda inp: fetch_pdf(inp["url"]),
}

# Word-bounded — plain substring matching misfires ("Starlink" → "link")
_FETCH_HINT_RE = re.compile(
    r"https?://|\.(?:ro|com|org|net|io)\b"
    r"|\b(?:hotnews|g4media|digi24|protv|antena|realitatea"
    r"|site|page|article|link|url|pdf)\b",
    re.IGNORECASE,
)


def _needs_fetch(message: str, history: list) -> bool:
    """Route to Claude if current message OR any recent history references specific sites."""
    combined = message + " " + " ".join(t for _, t in history)
    return bool(_FETCH_HINT_RE.search(combined))


def _system_prompt(claude_tools: bool = True) -> str:
    """Base prompt. claude_tools=True adds Claude-specific tool rules — never send
    those to Grok/Gemini, which lack web_search/fetch_pdf and will echo tool calls
    as literal text."""
    today = date.today().strftime("%B %d, %Y")
    tool_rules = """
TOOL RULES:
- PDF requests: use web_search to find the direct PDF URL, then call fetch_pdf. Never summarize from snippets alone.
- Direct .pdf URLs: call fetch_pdf immediately.
- All other questions: use web_search.
""" if claude_tools else ""
    return f"""You are a concise assistant on Telegram.
Today's date is {today}. Use this when interpreting "this year", "today", "recently", etc.
Small screen, limited WiFi. Every character counts.
{tool_rules}
OUTPUT RULES:
- Max {settings.max_response_chars} characters. Plain text only. No markdown, no bullets, no asterisks.
- No preamble. Answer directly.
- PDF: 3-5 key points separated by " | ".
- Score: "Barcelona 2 - Real Madrid 1. Final."
- Never apologize or explain your process.
- Never reply with a description of a search you would do — always answer with the information itself.
- Image requests are handled separately. Never say you cannot show images."""


# ── Gemini (primary — free, google_search server-side) ───────────────────────

_gemini = genai.Client(api_key=settings.gemini_api_key)
_gemini_models = [m.strip() for m in settings.gemini_models.split(",")]

GEMINI_TOOLS = [gtypes.Tool(google_search=gtypes.GoogleSearch())]


async def _run_gemini(user_message: str, history: list, preferred_model: str = None) -> tuple[str, str]:
    system = _system_prompt(claude_tools=False) + "\n\nYou have Google Search — use it for anything current or factual. No PDF fetching — for PDF requests summarize from search results as best you can."
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


async def _run_claude(
    user_message: str, history: list, model: str = None, attachments_out: list = None
) -> tuple[str, str]:
    """attachments_out: pass a list to receive the first fetched PDF as a document attachment."""
    model = model or settings.claude_model
    system = _system_prompt()
    messages = [{"role": role, "content": text} for role, text in history]
    messages.append({"role": "user", "content": user_message})

    for iteration in range(settings.max_tool_iterations):
        response = await _claude.messages.create(
            model=model,
            max_tokens=512,
            system=system,
            tools=CLAUDE_TOOLS,
            messages=messages,
        )
        logger.info("Claude stop_reason=%s iter=%d", response.stop_reason, iteration)
        _track_claude(response, model)

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
                    if block.name == "fetch_pdf" and attachments_out is not None and not attachments_out:
                        try:
                            pdf_bytes, filename = await fetch_pdf_bytes(block.input["url"])
                            if len(pdf_bytes) < 50 * 1024 * 1024:  # Telegram document limit
                                attachments_out.append({"type": "document", "data": pdf_bytes, "filename": filename})
                        except Exception as exc:
                            logger.warning("PDF attachment fetch failed: %s", exc)
                except Exception as exc:
                    logger.warning("Tool %s failed: %s", block.name, exc)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {exc}", "is_error": True})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        break

    return "Could not complete that request.", "claude"


# ── Grok (xAI — free-form via /grok command) ────────────────────────────────

GROK_MODEL = "grok-4.3"

# Grok cites sources as markdown [[1]](url) — strip for plain-text Telegram
_GROK_CITATION_RE = re.compile(r"\s*\[\[\d+\]\]\(\S+?\)")


async def _run_grok(user_message: str, history: list) -> str:
    system = _system_prompt(claude_tools=False)
    messages = [{"role": "assistant" if r == "assistant" else "user", "content": t} for r, t in history]
    messages.append({"role": "user", "content": user_message})
    # /v1/responses with server-side web_search — Live Search on chat/completions is 410 Gone
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.x.ai/v1/responses",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.xai_api_key}"},
            json={
                "model": GROK_MODEL,
                "input": [{"role": "system", "content": system}] + messages,
                "tools": [{"type": "web_search"}],
                "max_output_tokens": 1024,
            },
        )
        resp.raise_for_status()
    data = resp.json()
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    parts.append(part["text"])
    text = _GROK_CITATION_RE.sub("", " ".join(parts)).strip()
    usage = data.get("usage", {})
    ticks = usage.get("cost_in_usd_ticks")
    _track_cost(
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        GROK_MODEL,
        cost_usd=ticks / 1e10 if ticks else None,
    )
    if len(text) > settings.max_response_chars:
        text = text[:settings.max_response_chars].rsplit(" ", 1)[0] + "…"
    return text or "No answer found."


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
        _track_claude(response, settings.claude_model)
        all_text = " ".join(b.text for b in response.content if hasattr(b, "text"))
        urls = _IMAGE_URL_RE.findall(all_text)
        urls = [u for u in urls if "/thumb/" not in u] or urls
    except Exception as exc:
        logger.warning("Claude image search failed: %s", exc)
        urls = []

    for url in urls[:5]:
        try:
            ua = _WIKI_UA if "wikimedia" in url or "wikipedia" in url else "Mozilla/5.0"
            async with public_client(timeout=10, follow_redirects=True) as client:
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

_WEATHER_TIME_WORDS = {
    "this", "week", "weekend", "today", "tonight", "tomorrow",
    "forecast", "next", "days", "hourly", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
}


async def cmd_weather(args: str) -> str:
    query = args.strip() or "Bucharest"
    # wttr.in only does current conditions and fuzzy-matches extra words as city
    # names ("Bucharest this week" → Wick, UK) — send time-qualified queries to AI
    if any(w in _WEATHER_TIME_WORDS for w in query.lower().split()):
        answer, _ = await _run_gemini(f"Weather forecast: {query}. Concise, plain text.", [])
        return answer
    try:
        return await get_weather(query)
    except Exception as exc:
        return f"Weather unavailable: {exc}"


async def cmd_wiki(args: str) -> str:
    if not args.strip():
        return "Usage: /wiki <topic>"
    topic = args.strip()
    try:
        text, usage = await grokipedia(topic, settings.max_response_chars)
        _track_cost(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), GROK_MODEL)
        return text
    except Exception as exc:
        logger.warning("Grok wiki failed: %s", exc)
    # Free Wikipedia fallback
    try:
        summary = await get_wiki(topic)
    except Exception as exc:
        return f"Wiki unavailable: {exc}"
    if not summary:
        return f"No wiki entry found for: {topic}"
    if len(summary) > settings.max_response_chars:
        summary = summary[: settings.max_response_chars].rsplit(" ", 1)[0] + "…"
    return summary


async def cmd_flight(args: str) -> str:
    if not args.strip():
        return "Usage: /flight <number>  e.g. /flight LH441"
    number = args.strip().upper()
    # Live flight API when configured, AI search otherwise
    if settings.aviationstack_api_key:
        try:
            return await flight_status(number)
        except Exception as exc:
            logger.warning("aviationstack failed, falling back to Gemini: %s", exc)
    prompt = f"Current status of flight {number}. One line: flight number, status, departure airport+time, arrival airport+time, any delay."
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
    answer, _ = await _run_claude(prompt, [], attachments_out=attachments)
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
            results.append(await asyncio.to_thread(get_stock, ticker))
        except Exception as exc:
            results.append(f"{ticker.upper()}: error ({exc})")
    return "\n".join(results)


async def cmd_search(args: str) -> str:
    if not args.strip():
        return "Usage: /search <query>"
    prompt = (
        f"Search the web for: {args.strip()}\n"
        "Return the most relevant result. Include: title, URL, and a 2-3 sentence summary. "
        "Plain text, no markdown."
    )
    answer, _ = await _run_claude(prompt, [])
    return answer


async def cmd_sports(args: str) -> str:
    if not args.strip():
        return "Usage: /sports <topic>  e.g. /sports world cup"
    prompt = (
        f"Latest results and scores for: {args.strip()}. "
        "Include scores, dates, upcoming matches if relevant. Plain text, concise."
    )
    try:
        answer, _ = await _run_gemini(prompt, [])
    except Exception:
        answer, _ = await _run_claude(prompt, [])
    return answer


async def cmd_retrieve(args: str) -> tuple[str, list]:
    if not args.strip():
        return "Usage: /retrieve <work>  e.g. /retrieve shakespeare sonnet 43", []
    query = args.strip()
    try:
        text = await _retrieve_via_model(query)
    except Exception as exc:
        # Anthropic's output filter blocks long verbatim reproduction even for
        # public-domain works — fetch the text from a source URL instead
        logger.warning("Model retrieve failed (%s), trying source fetch", str(exc)[:80])
        text = ""
    if not text or text.lower().startswith(("i'm not able", "i can't", "i cannot")):
        text = await _retrieve_via_source(query)
    if not text:
        return f"Couldn't retrieve: {query}", []
    attachments = _maybe_pdf(text, query)
    return text, attachments


async def _retrieve_via_model(query: str) -> str:
    prompt = (
        f"Retrieve the full text of: {query}. "
        "Return the complete original text — do not summarize or truncate. "
        "Include title and author at the top. Plain text, no markdown."
    )
    response = await _claude.messages.create(
        model=settings.claude_model, max_tokens=4096,
        system=(
            "You retrieve and reproduce full texts of literary works. "
            "Classic works published before 1930 (Shakespeare, Kipling, Eminescu, etc.) "
            "are in the public domain — reproduce them in full without hesitation. "
            "Only decline works still under copyright. Return complete text, never summarize."
        ),
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    _track_claude(response, settings.claude_model)
    return " ".join(b.text for b in response.content if hasattr(b, "text")).strip()


_URL_RE = re.compile(r"https?://\S+")


async def _retrieve_via_source(query: str) -> str:
    response = await _claude.messages.create(
        model=settings.claude_model, max_tokens=300,
        system=(
            "Find URLs of the complete text of the requested work on public-domain "
            "sources. Strongly prefer en.wikisource.org and gutenberg.org (plain-text "
            ".txt links best). Avoid poetryfoundation.org — it blocks bots. "
            "Return up to 3 URLs, one per line, nothing else."
        ),
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": query}],
    )
    _track_claude(response, settings.claude_model)
    all_text = " ".join(b.text for b in response.content if hasattr(b, "text"))
    for url in _URL_RE.findall(all_text)[:3]:
        url = url.rstrip(").,]")
        logger.info("Retrieve source fetch: %s", url)
        try:
            text = await fetch_url(url, max_chars=30000)
            if len(text) > 200:
                return await _extract_work(text, query)
        except Exception as exc:
            logger.warning("Source fetch failed: %s", exc)
    return ""


async def _extract_work(page_text: str, query: str) -> str:
    """Haiku pass: strip site chrome from a fetched page, keep only the work itself."""
    try:
        response = await _claude.messages.create(
            model="claude-haiku-4-5", max_tokens=4096,
            system=(
                "The user gives you a raw web page dump containing a public-domain literary "
                "work. Output ONLY the work: title, author, then its complete text with "
                "proper line breaks (verse lines for poems). Remove navigation, licensing, "
                "categories, and all other site chrome. No commentary."
            ),
            messages=[{"role": "user", "content": f"Work requested: {query}\n\nPage dump:\n{page_text}"}],
        )
        _track_claude(response, "claude-haiku-4-5")
        cleaned = " ".join(b.text for b in response.content if hasattr(b, "text")).strip()
        return cleaned if len(cleaned) > 100 else page_text
    except Exception as exc:
        logger.warning("Work extraction failed, returning raw text: %s", exc)
        return page_text


def _maybe_pdf(text: str, title: str) -> list:
    """If text exceeds comfortable Telegram reading length, also send as PDF."""
    if len(text) <= 1500:
        return []
    try:
        from tools.pdf import text_to_pdf
        pdf_bytes = text_to_pdf(text, title)
        filename = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:40] + ".pdf"
        return [{"type": "document", "data": pdf_bytes, "filename": filename}]
    except Exception as exc:
        logger.warning("PDF generation failed: %s", exc)
        return []


async def cmd_grok(args: str) -> str:
    if not args.strip():
        return "Usage: /grok <question>"
    return await _run_grok(args.strip(), [])


_CLAUDE_MODELS = {
    "claude": settings.claude_model,
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-8",
    "haiku":  "claude-haiku-4-5",
}


async def _run_claude_model(question: str, model: str) -> str:
    text, _ = await _run_claude(question, [], model)
    return text


async def cmd_claude(args: str) -> str:
    if not args.strip():
        return "Usage: /claude <question>"
    return await _run_claude_model(args.strip(), _CLAUDE_MODELS["claude"])


async def cmd_opus(args: str) -> str:
    if not args.strip():
        return "Usage: /opus <question>"
    return await _run_claude_model(args.strip(), _CLAUDE_MODELS["opus"])


async def cmd_sonnet(args: str) -> str:
    if not args.strip():
        return "Usage: /sonnet <question>"
    return await _run_claude_model(args.strip(), _CLAUDE_MODELS["sonnet"])


async def cmd_haiku(args: str) -> str:
    if not args.strip():
        return "Usage: /haiku <question>"
    return await _run_claude_model(args.strip(), _CLAUDE_MODELS["haiku"])


async def cmd_gemini(args: str) -> str:
    if not args.strip():
        return "Usage: /gemini <question>"
    answer, _ = await _run_gemini(args.strip(), [])
    return answer


async def cmd_cost(args: str) -> str:
    return get_daily_cost()


COMMANDS = {
    "weather":  cmd_weather,
    "wiki":     cmd_wiki,
    "flight":   cmd_flight,
    "news":     cmd_news,
    "pdf":      cmd_pdf,
    "image":    cmd_image,
    "tr":       cmd_tr,
    "stocks":   cmd_stocks,
    "search":   cmd_search,
    "sports":   cmd_sports,
    "retrieve": cmd_retrieve,
    "cost":     cmd_cost,
    "grok":     cmd_grok,
    "claude":   cmd_claude,
    "opus":     cmd_opus,
    "sonnet":   cmd_sonnet,
    "haiku":    cmd_haiku,
    "gemini":   cmd_gemini,
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

    # Explicit model routing
    if preferred_model == "claude":
        logger.info("Routing to Claude (user choice)")
        atts = []
        text, model = await _run_claude(user_message, history, attachments_out=atts)
        return text, model, atts

    if preferred_model == "grok":
        logger.info("Routing to Grok (user choice)")
        text = await _run_grok(user_message, history)
        return text, "grok", []

    if preferred_model == "gemini":
        logger.info("Routing to Gemini (user choice)")
        text, model = await _run_gemini(user_message, history)
        return text, model, []

    if _needs_fetch(user_message, history):
        logger.info("Routing to Claude (fetch needed)")
        atts = []
        text, model = await _run_claude(user_message, history, attachments_out=atts)
        return text, model, atts
    try:
        text, model = await _run_gemini(user_message, history, preferred_model)
        return text, model, []
    except Exception as exc:
        logger.warning("Gemini failed (%s), falling back to Claude", str(exc)[:60])
        atts = []
        text, model = await _run_claude(user_message, history, attachments_out=atts)
        return text, model, atts
