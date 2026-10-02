"""Manual live Telegram check (operator-run only: ``python -m tests.test_telegram``).

Guarded by ``__main__`` so that importing or collecting this module (pytest) never sends a message.
"""
from alert_engine.telegram_notifier import send_telegram_alert


message = """
MIAS TEST ALERT

Ticker: NVDA
Impact: 85/100
Status: Telegram integration working
"""


def main():
    result = send_telegram_alert(message)
    print(result)


if __name__ == "__main__":
    main()
