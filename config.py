import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_STRING: str = ""
    TELEGRAM_CHANNEL: str = ""
    TELEGRAM_BOT_TOKENS: str = ""
    PORT: int = 3000
    URL_SECRET: Optional[str] = None
    PUBLIC_URL: Optional[str] = None
    ENABLE_CLOUDFLARE_TUNNEL: bool = True

    def validate_for_main(self) -> None:
        """Validate required configuration settings for running the main server."""
        errors: list[str] = []
        if not self.TELEGRAM_API_ID:
            errors.append("TELEGRAM_API_ID is required (int)")
        if not self.TELEGRAM_API_HASH:
            errors.append("TELEGRAM_API_HASH is required (str)")
        if not self.TELEGRAM_SESSION_STRING:
            errors.append("TELEGRAM_SESSION_STRING is required (run login.py to generate)")
        if not self.TELEGRAM_CHANNEL:
            errors.append("TELEGRAM_CHANNEL is required (str)")

        if errors:
            raise ValueError(
                "Missing required environment variables:\n - " + "\n - ".join(errors)
            )

    @property
    def bot_tokens(self) -> list[str]:
        """Parsed list of bot tokens from TELEGRAM_BOT_TOKENS (up to 20 tokens)."""
        if not self.TELEGRAM_BOT_TOKENS:
            return []
        tokens = [
            t.strip().strip('"').strip("'")
            for t in self.TELEGRAM_BOT_TOKENS.split(",")
            if t.strip().strip('"').strip("'")
        ]
        return tokens[:20]

    @property
    def clean_url_secret(self) -> str:
        """Sanitized URL secret prefix without leading or trailing slashes."""
        if not self.URL_SECRET:
            return ""
        return self.URL_SECRET.strip().strip("/").strip('"').strip("'")

    @property
    def clean_public_url(self) -> str:
        """Sanitized public URL without trailing slashes."""
        if not self.PUBLIC_URL:
            return ""
        return self.PUBLIC_URL.strip().rstrip("/").strip('"').strip("'")


config = Settings()
