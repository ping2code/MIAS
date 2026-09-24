"""Pure, dotenv-free parsing of bounded shadow-persistence queue sizes (Phase 2S-A).

Used by ``shared.config`` (fail fast at collector startup, like other settings)
and by ``persistence.treasury_shadow`` when it lazily builds its writer from the
same process environment. Only Treasury has a family-specific size; every other
family keeps the shared default writer capacity.
"""

SHARED_DEFAULT_QUEUE_SIZE = 64
TREASURY_DEFAULT_QUEUE_SIZE = 256  # Measured healthy live Treasury burst: ~115 submissions per cycle.
MAX_QUEUE_SIZE = 4096  # The shadow writer's own upper bound; queues are never unbounded.


def treasury_queue_size(environ):
    """``TREASURY_PERSISTENCE_QUEUE_SIZE`` as a bounded integer (64..4096); ValueError otherwise."""
    raw = environ.get("TREASURY_PERSISTENCE_QUEUE_SIZE", str(TREASURY_DEFAULT_QUEUE_SIZE))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("TREASURY_PERSISTENCE_QUEUE_SIZE must be an integer") from None
    if not SHARED_DEFAULT_QUEUE_SIZE <= value <= MAX_QUEUE_SIZE:
        raise ValueError(f"TREASURY_PERSISTENCE_QUEUE_SIZE must be between {SHARED_DEFAULT_QUEUE_SIZE} and {MAX_QUEUE_SIZE}")
    return value
