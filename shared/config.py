import os
from dotenv import load_dotenv

load_dotenv()

ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "70"))
DISPLAY_THRESHOLD = int(os.getenv("DISPLAY_THRESHOLD", "40"))

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

NEAR_DUPLICATE_THRESHOLD = float(
    os.getenv("NEAR_DUPLICATE_THRESHOLD", "0.80")
)

HEADLINE_TTL_SECONDS = int(
    os.getenv("HEADLINE_TTL_SECONDS", "86400")
)

DEDUP_TTL_SECONDS = int(
    os.getenv("DEDUP_TTL_SECONDS", "86400")
)

RSS_ENTRY_LIMIT = int(
    os.getenv("RSS_ENTRY_LIMIT", "10")
)

FED_MAX_AGE_HOURS = int(os.getenv("FED_MAX_AGE_HOURS", "48"))
if FED_MAX_AGE_HOURS <= 0:
    raise ValueError("FED_MAX_AGE_HOURS must be positive")

MACRO_MAX_AGE_HOURS = int(os.getenv("MACRO_MAX_AGE_HOURS", "48"))
if MACRO_MAX_AGE_HOURS <= 0:
    raise ValueError("MACRO_MAX_AGE_HOURS must be positive")

TREASURY_MAX_AGE_HOURS = int(os.getenv("TREASURY_MAX_AGE_HOURS", "48"))
TREASURY_YIELD_MOVE_BPS = float(os.getenv("TREASURY_YIELD_MOVE_BPS", "15"))
if TREASURY_MAX_AGE_HOURS <= 0:
    raise ValueError("TREASURY_MAX_AGE_HOURS must be positive")
if not 0 < TREASURY_YIELD_MOVE_BPS < float("inf"):
    raise ValueError("TREASURY_YIELD_MOVE_BPS must be positive and finite")
