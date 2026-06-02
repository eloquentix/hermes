import logging
from datetime import date
import anthropic
from google import genai
from google.genai import types as gtypes

from config import settings
from tools.fetch import fetch_url, SCHEMA as FETCH_SCHEMA
from tools.pdf import fetch_pdf, SCHEMA as PDF_SCHEMA
from tools.weather import get_weather
from tools.grok import grokipedia
from tools.stocks import get_stock

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
    return f"""You are a concise assistant on Telegram for airplane flights.
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
- Never apologize or explain your process."""


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


async def cmd_pdf(args: str) -> str:
    if not args.strip():
        return "Usage: /pdf <url or document name>"
    prompt = f"Fetch and summarize this PDF: {args.strip()}"
    answer, _ = await _run_claude(prompt, [])
    return answer


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
    "tr":      cmd_tr,
    "stocks":  cmd_stocks,
}


# ── Public entry point ────────────────────────────────────────────────────────

async def run_agent(user_message: str, history: list = None, preferred_model: str = None) -> tuple[str, str]:
    history = history or []
    if _needs_fetch(user_message, history):
        logger.info("Routing to Claude (fetch needed)")
        return await _run_claude(user_message, history)
    try:
        return await _run_gemini(user_message, history, preferred_model)
    except Exception as exc:
        logger.warning("Gemini failed (%s), falling back to Claude", str(exc)[:60])
        return await _run_claude(user_message, history)
