"""Legacy Telegram notifier used by the collectors (unchanged interface).

Phase 12D hardening: this module no longer loads ``.env``. Settings come from the process environment, which the
collectors already prepare through ``shared.config`` before importing it. Errors carry only the exception type, never
the request URL, which contains the bot token.
"""
import os
import time
import requests


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


def send_telegram_alert(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing"
        )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10
            )

            response.raise_for_status()

            return response.json()

        except requests.RequestException as error:
            last_error = type(error).__name__        # never the message: requests errors include the token URL

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Telegram delivery failed after "
        f"{MAX_RETRIES} attempts ({last_error})"
    )
