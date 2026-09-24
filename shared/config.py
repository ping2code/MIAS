import os
from dotenv import load_dotenv

from shared.queue_settings import treasury_queue_size

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
FED_PERSISTENCE_SHADOW_ENABLED = os.getenv("FED_PERSISTENCE_SHADOW_ENABLED", "false").lower() == "true"
if FED_MAX_AGE_HOURS <= 0:
    raise ValueError("FED_MAX_AGE_HOURS must be positive")

MACRO_MAX_AGE_HOURS = int(os.getenv("MACRO_MAX_AGE_HOURS", "48"))
MACRO_PERSISTENCE_SHADOW_ENABLED = os.getenv("MACRO_PERSISTENCE_SHADOW_ENABLED", "false").lower() == "true"
if MACRO_MAX_AGE_HOURS <= 0:
    raise ValueError("MACRO_MAX_AGE_HOURS must be positive")

TREASURY_MAX_AGE_HOURS = int(os.getenv("TREASURY_MAX_AGE_HOURS", "48"))
TREASURY_YIELD_MOVE_BPS = float(os.getenv("TREASURY_YIELD_MOVE_BPS", "15"))
TREASURY_PERSISTENCE_SHADOW_ENABLED = os.getenv("TREASURY_PERSISTENCE_SHADOW_ENABLED", "false").lower() == "true"
# Bounded shadow queue for Treasury's larger healthy burst (other families keep the shared default of 64).
TREASURY_PERSISTENCE_QUEUE_SIZE = treasury_queue_size(os.environ)
if TREASURY_MAX_AGE_HOURS <= 0:
    raise ValueError("TREASURY_MAX_AGE_HOURS must be positive")
if not 0 < TREASURY_YIELD_MOVE_BPS < float("inf"):
    raise ValueError("TREASURY_YIELD_MOVE_BPS must be positive and finite")

GEOPOLITICAL_MAX_AGE_HOURS = int(os.getenv("GEOPOLITICAL_MAX_AGE_HOURS", "48"))
GEOPOLITICAL_ALIAS_TTL_DAYS = int(os.getenv("GEOPOLITICAL_ALIAS_TTL_DAYS", "365"))
GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED = os.getenv("GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED", "false").lower() == "true"
GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED = os.getenv("GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED", "false").lower() == "true"
GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS = int(os.getenv("GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS", "250"))
if not 10 <= GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS <= 5000:
    raise ValueError("GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS must be between 10 and 5000")
GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED = os.getenv("GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED", "false").lower() == "true"
GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS = int(os.getenv("GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS", "300"))
if not 10 <= GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS <= 86400:
    raise ValueError("GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS must be between 10 and 86400")
if GEOPOLITICAL_MAX_AGE_HOURS <= 0 or GEOPOLITICAL_ALIAS_TTL_DAYS * 24 <= GEOPOLITICAL_MAX_AGE_HOURS:
    raise ValueError("Geopolitical alias retention must exceed positive freshness window")

SEC_PERSISTENCE_SHADOW_ENABLED = os.getenv("SEC_PERSISTENCE_SHADOW_ENABLED", "false").lower() == "true"
NEWS_PERSISTENCE_SHADOW_ENABLED = os.getenv("NEWS_PERSISTENCE_SHADOW_ENABLED", "false").lower() == "true"
