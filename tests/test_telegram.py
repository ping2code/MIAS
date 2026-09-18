from alert_engine.telegram_notifier import send_telegram_alert


message = """
MIAS TEST ALERT

Ticker: NVDA
Impact: 85/100
Status: Telegram integration working
"""

result = send_telegram_alert(message)

print(result)
