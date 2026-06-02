# Inflight AI Bot

A personal Telegram bot designed for use on airplane inflight WiFi (Lufthansa, Swiss, etc. offer free Telegram). Send natural language messages or slash commands, get concise answers back — optimized for small screens and limited bandwidth.

## What it does

- **Natural language queries** — web search via Gemini (Google Search built-in) or Claude
- **PDF fetch + summarize** — paste a PDF URL, get 3–5 key points
- **Weather, stocks, news, flight status, translation** via slash commands
- **Conversation memory** — 12-turn context per chat, cleared with `/clear`
- **Single-user** — locked to your Telegram user ID; everyone else gets rejected

## Commands

```
/weather <city>         current weather (wttr.in, free, no key)
/flight <LH441>         live flight status via web search
/news [topic]           top 5 headlines
/pdf <url or name>      fetch and summarize a PDF
/wiki <topic>           Grokipedia-style encyclopedia summary via Grok
/stocks <AAPL TSLA>     live stock quotes (yfinance, free, no key)
/tr <lang> <text>       translate
/clear                  reset conversation context
/help                   command list
```

## Architecture

```
Telegram app (phone)
  → Telegram servers          (inflight WiFi carries this leg)
  → POST /webhook (FastAPI)   (secret token validated)
  → handle_message()          (user ID guard)
  → run_agent()               (routing logic)
       ├─ URL/site/PDF queries → Claude (web_search + fetch_url + fetch_pdf)
       └─ everything else     → Gemini (google_search built-in)
                                  └─ 4xx/quota → fallback to Claude
  → reply via Bot API
```

**Key constraint**: Gemini's API does not allow mixing `google_search` with custom function declarations in the same request. This is why routing is split — Gemini handles general queries with its built-in search, Claude handles anything requiring URL or PDF fetching.

## Stack

| Component | Purpose | Cost |
|---|---|---|
| [python-telegram-bot 22.x](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram webhook handling | Free |
| [FastAPI](https://fastapi.tiangolo.com) + uvicorn | Webhook server | Free |
| [Google Gemini](https://aistudio.google.com) | Primary AI — built-in Google Search | Free tier |
| [Anthropic Claude](https://console.anthropic.com) | Fallback + PDF/URL fetching | Pay-per-use |
| [xAI Grok](https://console.x.ai) | /wiki encyclopedia summaries | Pay-per-use |
| [yfinance](https://github.com/ranaroussi/yfinance) | Stock quotes | Free |
| [wttr.in](https://wttr.in) | Weather | Free |
| [PyMuPDF](https://pymupdf.readthedocs.io) | PDF text extraction | Free |

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/botfather) → `/newbot` → follow prompts → save the token
2. Message [@userinfobot](https://t.me/userinfobot) → note your numeric user ID

### 2. Get API keys

| Key | Where |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) → Get API key |
| `XAI_API_KEY` | [console.x.ai](https://console.x.ai) |

### 3. Configure

```bash
cp .env.example .env
# Edit .env — fill in all values
# Generate a webhook secret:
python -c "import secrets; print(secrets.token_hex(32))"
```

### 4. Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## Deployment options

### Option A — Local machine + ngrok (development / testing)

Fastest way to get running. Free ngrok account gives you a stable domain that survives restarts.

```bash
# Install ngrok: https://ngrok.com/download
ngrok config add-authtoken <your-token>
ngrok http 8000
# Copy the https URL → set as WEBHOOK_BASE_URL in .env (no trailing slash)
uvicorn main:app --reload
```

### Option B — Raspberry Pi (always-on, home network)

Run on any Pi (tested on Pi 4/5). Expose via a free dynamic DNS service.

**DuckDNS setup** (free, `your-name.duckdns.org`):
```bash
# On the Pi — update IP every 5 minutes
echo "*/5 * * * * curl -s 'https://www.duckdns.org/update?domains=YOUR_DOMAIN&token=YOUR_TOKEN&ip=' > /dev/null" | crontab -
```

**nginx on port 8443** (Telegram supports 443, 80, 88, 8443):
```nginx
server {
    listen 8443 ssl;
    server_name your-name.duckdns.org;

    ssl_certificate     /etc/letsencrypt/live/your-name.duckdns.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-name.duckdns.org/privkey.pem;

    location /webhook {
        proxy_pass http://127.0.0.1:8000;
    }
    location /health {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

**Let's Encrypt via DNS challenge** (no port 80 needed):
```bash
# Install certbot + DuckDNS plugin
pip install certbot certbot-dns-duckdns
certbot certonly \
  --authenticator dns-duckdns \
  --dns-duckdns-token YOUR_DUCKDNS_TOKEN \
  -d your-name.duckdns.org
```

**systemd service** (`/etc/systemd/system/proxy.service`):
```ini
[Unit]
Description=Inflight Telegram Bot
After=network.target

[Service]
WorkingDirectory=/home/pi/dev/proxy
ExecStart=/home/pi/dev/proxy/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable proxy
sudo systemctl start proxy
```

### Option C — Fly.io (cloud, free tier)

Zero infrastructure to manage. Free tier covers a small always-on instance.

```bash
# Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
fly auth login
fly launch          # creates fly.toml, detects Dockerfile
fly secrets set \
  TELEGRAM_TOKEN=... \
  ANTHROPIC_API_KEY=... \
  GEMINI_API_KEY=... \
  XAI_API_KEY=... \
  TELEGRAM_ALLOWED_USER_ID=... \
  WEBHOOK_BASE_URL=https://your-app-name.fly.dev \
  WEBHOOK_SECRET_TOKEN=...
fly deploy
```

The `WEBHOOK_BASE_URL` will be `https://your-app-name.fly.dev` (port 443, standard HTTPS).

Update `Dockerfile` CMD for Fly:
```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Option D — Railway.app

Similar to Fly, $5/month on the hobby plan. Connect your GitHub repo → set env vars in the dashboard → auto-deploys on push.

Set `PORT` to `8080` and `WEBHOOK_BASE_URL` to your Railway-provided domain.

---

## Project structure

```
proxy/
├── main.py           # FastAPI app, webhook endpoint, lifespan startup
├── bot.py            # Telegram handlers, per-chat history + model stickiness
├── agent.py          # AI routing, Gemini + Claude loops, slash command handlers
├── config.py         # All env vars via pydantic-settings
├── tools/
│   ├── fetch.py      # Generic URL → text (used by Claude)
│   ├── pdf.py        # PDF download + PyMuPDF extraction
│   ├── weather.py    # wttr.in weather
│   ├── grok.py       # xAI API for /wiki
│   └── stocks.py     # yfinance stock quotes
├── requirements.txt
├── .env.example
├── Dockerfile
└── .gitignore
```

## Security notes

- **User ID guard**: only your Telegram user ID can interact with the bot. All other senders receive "Unauthorized." immediately.
- **Webhook secret**: every POST to `/webhook` must include the correct `X-Telegram-Bot-Api-Secret-Token` header. Requests without it return 403.
- **No credentials in code**: all secrets are environment variables, loaded via pydantic-settings. Never commit `.env`.
- **Tool loop cap**: `MAX_TOOL_ITERATIONS=5` prevents runaway agent loops.

## Cost expectations

Typical usage (10–20 queries/flight):
- **Gemini**: free tier (generous daily quota) covers most general queries
- **Claude**: fallback only; at `claude-sonnet-4-6` pricing, ~$0.01–0.03 per query
- **Grok**: `/wiki` only; negligible usage cost

Stay well within free tiers on a typical flight.
