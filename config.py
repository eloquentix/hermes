from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram
    telegram_token: str
    telegram_allowed_user_id: int

    # Webhook
    webhook_base_url: str
    webhook_secret_token: str

    # Claude (primary — paid, native web search + PDF)
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-6"

    # Gemini (fallback — free tier, PDF only)
    gemini_api_key: str
    gemini_models: str = "gemini-2.5-flash-lite,gemini-2.5-flash"

    # xAI / Grok (for /wiki)
    xai_api_key: str

    # Google Custom Search (for /image)
    google_cse_api_key: str = ""
    google_cse_cx: str = ""

    # Behavior
    max_response_chars: int = 280
    max_tool_iterations: int = 5


settings = Settings()
