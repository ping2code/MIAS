"""Technical snapshot persistence settings from the process environment only (never ``.env``) — Phase 4C.

| Variable | Default | Range |
|---|---|---|
| ``TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED`` | false | true/false |
| ``TECHNICAL_SNAPSHOT_QUEUE_SIZE`` | 256 | 16-4096 |
| ``TECHNICAL_SNAPSHOT_ENGINE_VERSION`` | ``phase4c-v1`` | 1-32 of ``[a-z0-9.-]``, starting alphanumeric |
| ``TECHNICAL_SNAPSHOT_DRAIN_TIMEOUT_SECONDS`` | 10 | 0-30 |

Persistence is never alert- or output-critical. There is deliberately no setting
that makes a database failure fail the runner.
"""
from dataclasses import dataclass
import re

DEFAULT_ENGINE_VERSION = "phase4c-v1"
ENGINE_VERSION = re.compile(r"[a-z0-9][a-z0-9.\-]{0,31}")


class TechnicalPersistenceConfigError(ValueError):
    """Invalid technical persistence setting; the message names the setting only."""


@dataclass(frozen=True)
class TechnicalPersistenceSettings:
    enabled: bool
    queue_size: int
    engine_version: str
    drain_timeout_seconds: float


def load_technical_persistence_settings(environ):
    raw = (environ.get("TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED") or "false").strip().lower()
    if raw not in ("true", "false"):
        raise TechnicalPersistenceConfigError("TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED must be true or false")
    try:
        queue = int((environ.get("TECHNICAL_SNAPSHOT_QUEUE_SIZE") or "256").strip())
        drain = float((environ.get("TECHNICAL_SNAPSHOT_DRAIN_TIMEOUT_SECONDS") or "10").strip())
    except ValueError:
        raise TechnicalPersistenceConfigError("TECHNICAL_SNAPSHOT queue size/drain timeout must be numbers") from None
    if not 16 <= queue <= 4096:
        raise TechnicalPersistenceConfigError("TECHNICAL_SNAPSHOT_QUEUE_SIZE must be between 16 and 4096")
    if not 0 <= drain <= 30:
        raise TechnicalPersistenceConfigError("TECHNICAL_SNAPSHOT_DRAIN_TIMEOUT_SECONDS must be between 0 and 30")
    version = (environ.get("TECHNICAL_SNAPSHOT_ENGINE_VERSION") or DEFAULT_ENGINE_VERSION).strip()
    if not ENGINE_VERSION.fullmatch(version):
        raise TechnicalPersistenceConfigError("TECHNICAL_SNAPSHOT_ENGINE_VERSION must be 1-32 of [a-z0-9.-]")
    return TechnicalPersistenceSettings(raw == "true", queue, version, drain)
