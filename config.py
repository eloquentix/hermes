from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram
    telegram_token: str
    telegram_allowed_user_id: int

    # Webhook
    webhook_base_url: str
    webhook_secret_token: str

    # Gemini (primary — free tier, google_search built-in)
    gemini_api_key: str
    gemini_models: str = "gemini-2.5-flash-lite,gemini-2.5-flash"

    # Claude (paid — URL/PDF fetching, fallback for Gemini failures)
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-6"

    # xAI / Grok (for /wiki and /grok)
    xai_api_key: str

    # Google Custom Search (for /image)
    google_cse_api_key: str = ""
    google_cse_cx: str = ""

    # Aviationstack (optional — live /flight data; Gemini search fallback if unset)
    aviationstack_api_key: str = ""

    # Behavior
    max_response_chars: int = 280
    max_tool_iterations: int = 5


settings = Settings()
