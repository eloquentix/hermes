# Hermes

**A personal, single-user Telegram AI bot you self-host.** Text it questions. It replies with web search results, stock quotes, weather, PDF summaries, news, translations — all through normal Telegram messages. Runs on a Raspberry Pi at home or cheap cloud hosting.

One strong use case: **reliable answers on airplane WiFi without paying for internet.** Most airlines offer free Telegram messaging — Hermes turns that into a full AI assistant.

> *"I was tired of paying for inflight WiFi just to Google things. So I built an AI that answers through Telegram's free tier."*

---

## What it does

- **Ask anything** — web search powered by Gemini (Google Search built-in), Claude as fallback
- **Fetch + summarize PDFs** — paste a URL, get key points back + the file itself
- **Image search** — find and send images via Google CSE or Wikipedia
- **Slash commands** — weather, stocks, news, flight status, wiki, translation
- **Conversation memory** — 12-turn context per chat
- **Owner-locked** — only your Telegram user ID can use it; everyone else gets rejected
- **Tiny footprint** — runs on a Raspberry Pi, costs ~$0.02/day in API calls

### Commands

```
/weather <city>         current weather
/flight <LH441>         live flight status
/news [topic]           top 5 headlines
/pdf <url or name>      fetch, summarize + send PDF
/image <topic>          find and send an image
/wiki <topic>           encyclopedia summary (powered by Grok)
/stocks <AAPL TSLA>     live stock quotes
/tr <lang> <text>       translate
/clear                  reset conversation
/help                   command list
```

---

## Quick start

```bash
git clone https://github.com/eloquentix/hermes.git
cd hermes
cp .env.example .env        # fill in your keys (see below)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

You need a public HTTPS URL for Telegram's webhook. For local dev, use [ngrok](https://ngrok.com): `ngrok http 8000`, then set `WEBHOOK_BASE_URL` in `.env`.

### What you need

**1. Telegram bot** — message [@BotFather](https://t.me/botfather) → `/newbot` → save the token. Get your user ID from [@userinfobot](https://t.me/userinfobot).

**2. API keys:**

| Key | Where | Cost |
|---|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) | Free tier |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Pay-per-use |
| `XAI_API_KEY` | [console.x.ai](https://console.x.ai) | Pay-per-use |

**3. Webhook secret** — generate one: `python -c "import secrets; print(secrets.token_hex(32))"`

---

## How it works

```
You (Telegram app)
  → Telegram servers
  → POST /webhook (FastAPI)        webhook secret validated
  → handle_message()               user ID guard
  → run_agent()                    smart routing:
       ├─ URL/PDF queries  → Claude   (web_search + fetch_url + fetch_pdf)
       └─ everything else  → Gemini   (google_search built-in, free)
                               └─ on 4xx/quota → fallback to Claude
  → reply via Bot API
  → You
```

**Why two models?** Gemini's API doesn't allow mixing its built-in `google_search` with custom function declarations. So Gemini handles general queries (free, fast, has Google Search), and Claude handles anything that needs URL fetching or PDF extraction. Automatic fallback if Gemini hits rate limits.

Responses are capped at 280 characters by default — optimized for quick reads on small screens.

---

## Deploy it

### Raspberry Pi (recommended for always-on)

Tested on Pi 4/5. Use DuckDNS (free) for a public domain, nginx for TLS, systemd for auto-restart.

**DuckDNS** — free dynamic DNS:
```bash
echo "*/5 * * * * curl -s 'https://www.duckdns.org/update?domains=YOUR_DOMAIN&token=YOUR_TOKEN&ip=' > /dev/null" | crontab -
```

**nginx on port 8443** (Telegram natively supports 443, 80, 88, 8443):
```nginx
server {
    listen 8443 ssl;
    server_name your-name.duckdns.org;

    ssl_certificate     /etc/letsencrypt/live/your-name.duckdns.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-name.duckdns.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

**Let's Encrypt via DNS challenge** (no port 80/443 needed):
```bash
pip install certbot certbot-dns-duckdns
certbot certonly \
  --authenticator dns-duckdns \
  --dns-duckdns-token YOUR_DUCKDNS_TOKEN \
  -d your-name.duckdns.org
```

**systemd service** (`/etc/systemd/system/hermes.service`):
```ini
[Unit]
Description=Hermes Telegram Bot
After=network.target

[Service]
WorkingDirectory=/home/pi/hermes
ExecStart=/home/pi/hermes/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Fly.io (free tier, zero maintenance)

```bash
fly launch
fly secrets set TELEGRAM_TOKEN=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=... \
  XAI_API_KEY=... TELEGRAM_ALLOWED_USER_ID=... \
  WEBHOOK_BASE_URL=https://your-app.fly.dev WEBHOOK_SECRET_TOKEN=...
fly deploy
```

### Railway.app ($5/month, auto-deploy from GitHub)

Connect your repo, set env vars in the dashboard, done. Set `WEBHOOK_BASE_URL` to your Railway domain.

### Local + ngrok (development)

```bash
ngrok http 8000                    # copy the https URL
# set WEBHOOK_BASE_URL in .env
uvicorn main:app --reload
```

---

## Project structure

```
hermes/
├── main.py           FastAPI app, webhook endpoint, lifespan startup
├── bot.py            Telegram handlers, per-chat history + model stickiness
├── agent.py          AI routing, Gemini + Claude loops, slash command handlers
├── config.py         All env vars via pydantic-settings
├── tools/
│   ├── fetch.py      URL → text (used by Claude tool loop)
│   ├── pdf.py        PDF download + PyMuPDF text extraction
│   ├── weather.py    wttr.in (free, no key)
│   ├── grok.py       xAI API for /wiki
│   ├── images.py     Google CSE image search (optional) + Wikipedia fallback
│   └── stocks.py     yfinance (free, no key)
├── requirements.txt
├── Dockerfile
├── .env.example
└── .gitignore
```

## Stack

| Component | Purpose | Cost |
|---|---|---|
| [python-telegram-bot 22.x](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram webhook handling | Free |
| [FastAPI](https://fastapi.tiangolo.com) + uvicorn | Webhook server | Free |
| [Google Gemini](https://aistudio.google.com) | Primary AI + Google Search | Free tier |
| [Anthropic Claude](https://console.anthropic.com) | Fallback + PDF/URL tools | Pay-per-use |
| [xAI Grok](https://console.x.ai) | /wiki encyclopedia entries | Pay-per-use |
| [yfinance](https://github.com/ranaroussi/yfinance) | Stock quotes | Free |
| [wttr.in](https://wttr.in) | Weather data | Free |
| [PyMuPDF](https://pymupdf.readthedocs.io) | PDF text extraction | Free |

## Security

- **Owner-only access** — locked to a single Telegram user ID. All other users are rejected immediately.
- **Webhook secret** — every incoming POST is validated against `X-Telegram-Bot-Api-Secret-Token`. No secret, no access.
- **No secrets in code** — everything is in `.env`, loaded via pydantic-settings.
- **Agent loop cap** — `MAX_TOOL_ITERATIONS=5` prevents runaway tool calls.

## Cost

Most queries hit Gemini's free tier. Claude is fallback only (~$0.01–0.03 per query). Grok is used only for `/wiki`. Typical daily cost for moderate usage: under $0.05.

---

## License

MIT
