from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    # Telegram
    TELEGRAM_BOT_TOKEN: str
    ADMIN_TELEGRAM_ID: int = 0

    # Paystack
    PAYSTACK_SECRET_KEY: str

    # Deployment
    RENDER_EXTERNAL_URL: str
    DATABASE_URL: str

    # App
    CORS_ORIGINS: List[str] = ["*"]
    TEMPLATE_DIR: str = "templates"

    # Business Logic
    TRIAL_DAYS: int = 7
    SUBSCRIPTION_DAYS: int = 30
    SUBSCRIPTION_AMOUNT_KOBO: int = 300000 # ₦3000
    COMMISSION_RATE: float = 0.05 # 5%

    class Config:
        case_sensitive = True

settings = Settings()
