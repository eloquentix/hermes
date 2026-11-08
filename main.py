import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from telegram import Update

from bot import build_application
from config import settings

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO, and Telegram API URLs embed the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

application = build_application()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await application.initialize()
    webhook_url = f"{settings.webhook_base_url}/webhook"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=settings.webhook_secret_token,
        allowed_updates=["message"],
    )
    logger.info("Webhook registered: %s", webhook_url)
    await application.start()
    yield
    await application.stop()
    await application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=None),
):
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", settings.webhook_secret_token):
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    update = Update.de_json(body, application.bot)
    await application.update_queue.put(update)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}
