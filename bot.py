import io
import logging
from collections import deque
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from config import settings
from agent import run_agent, COMMANDS

logger = logging.getLogger(__name__)

# In-memory state per chat
_history: dict[int, deque] = {}
_model: dict[int, str] = {}   # locked model for each conversation
MAX_HISTORY = 12


def _get_history(chat_id: int) -> deque:
    if chat_id not in _history:
        _history[chat_id] = deque(maxlen=MAX_HISTORY)
    return _history[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != settings.telegram_allowed_user_id:
        await update.message.reply_text("Unauthorized.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    chat_id = update.effective_chat.id
    history = _get_history(chat_id)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    locked_model = _model.get(chat_id)
    logger.info("USER: %s", text)
    try:
        answer, used_model, attachments = await run_agent(text, list(history), locked_model)
        if chat_id not in _model and used_model:
            _model[chat_id] = used_model
            logger.info("Locked conversation %d to model: %s", chat_id, used_model)
    except Exception as exc:
        logger.exception("Agent error")
        answer = f"Error: {str(exc)[:120]}"
        used_model = None
        attachments = []
    logger.info("BOT [%s]: %s", used_model, answer)

    history.append(("user", text))
    history.append(("assistant", answer))

    await update.message.reply_text(answer)
    await _send_attachments(update, attachments)


async def _send_attachments(update: Update, attachments: list) -> None:
    for att in attachments:
        try:
            if att["type"] == "photo":
                caption = att.get("caption")
                if "data" in att:
                    photo = io.BytesIO(att["data"])
                    photo.name = att.get("filename", "image.jpg")
                    await update.message.reply_photo(photo, caption=caption)
                else:
                    await update.message.reply_photo(att["url"], caption=caption)
            elif att["type"] == "document":
                doc = io.BytesIO(att["data"])
                doc.name = att.get("filename", "file")
                await update.message.reply_document(doc, filename=att.get("filename", "file"))
        except Exception as exc:
            logger.warning("Failed to send attachment: %s", exc)


async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != settings.telegram_allowed_user_id:
        return
    chat_id = update.effective_chat.id
    _history.pop(chat_id, None)
    _model.pop(chat_id, None)
    await update.message.reply_text("Context cleared.")


async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != settings.telegram_allowed_user_id:
        return
    text = update.message.text or ""
    parts = text.lstrip("/").split(" ", 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    fn = COMMANDS.get(cmd)
    if not fn:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    logger.info("CMD /%s %s", cmd, args)
    try:
        result = await fn(args)
        # Commands can return str or (str, attachments)
        if isinstance(result, tuple):
            answer, attachments = result
        else:
            answer, attachments = result, []
    except Exception as exc:
        logger.exception("Command error")
        answer = f"Error: {str(exc)[:120]}"
        attachments = []
    logger.info("BOT [/%s]: %s", cmd, answer)
    # Feed command into conversation history for follow-up context
    chat_id = update.effective_chat.id
    history = _get_history(chat_id)
    history.append(("user", text))
    if answer:
        history.append(("assistant", answer))
        await update.message.reply_text(answer)
    elif attachments:
        history.append(("assistant", f"[Sent image: {args}]"))
    await _send_attachments(update, attachments)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != settings.telegram_allowed_user_id:
        return
    await update.message.reply_text(
        "/weather <city> — current weather\n"
        "/flight <LH441> — live flight status\n"
        "/news [topic] — top headlines\n"
        "/pdf <url or name> — fetch, summarize + send PDF\n"
        "/image <topic> — find and send an image\n"
        "/wiki <topic> — Grokipedia summary via Grok\n"
        "/stocks <AAPL TSLA ...> — live stock quotes\n"
        "/tr <lang> <text> — translate\n"
        "/clear — reset conversation\n"
        "/help — this list"
    )


def build_application() -> Application:
    from telegram.ext import CommandHandler
    app = (
        Application.builder()
        .token(settings.telegram_token)
        .updater(None)
        .build()
    )
    app.add_handler(CommandHandler("clear", handle_clear))
    app.add_handler(CommandHandler("help", handle_help))
    for cmd in COMMANDS:
        app.add_handler(CommandHandler(cmd, handle_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
