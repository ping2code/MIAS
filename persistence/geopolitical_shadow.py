"""Best-effort bounded geopolitical shadow writes; reuses the macro writer contract.

PostgreSQL is a historical record only: Redis alias/policy state is never read,
repaired or synchronized from here.
"""

from persistence import macro_shadow
from persistence.adapters.geopolitical import adapt_geopolitical, geopolitical_promotion
from persistence.database import transaction
from persistence.repository import EventRepository
from persistence.shadow_lifecycle import ShadowLifecycle
from shared.logger import get_logger

logger = get_logger("geopolitical_shadow")


def persist_geopolitical(engine, event, observed_at, *, make_current=True, report=False):
    """One atomic transaction: facts, evidence, then existing outcome snapshots."""
    adapted = adapt_geopolitical(event, observed_at, make_current=make_current)
    with transaction(engine) as session:
        repo = EventRepository(session)
        row = repo.record(**adapted["record"], promotion_policy=geopolitical_promotion)
        for key, values in adapted["provenance"]:
            repo.add_provenance(row["id"], key, **values)
        for kind, values in adapted["histories"]:
            repo.append_history(row["id"], kind, values)
    return {"version": row, "duplicate": repo.inserted_count == 0,
            "promotion": repo.promotion_reason} if report else row


def runtime_engine():
    return macro_shadow.runtime_engine(application_name="mias_geopolitical_shadow")


_MESSAGES = {key: value.replace("Macro shadow", "Geopolitical shadow")
             for key, value in macro_shadow._MESSAGES.items()}


class GeopoliticalShadowWriter(macro_shadow.ShadowWriter):
    """Same queue, counters, logging bounds and shutdown semantics as macro."""
    logger = logger
    messages = _MESSAGES
    thread_name = "mias-geopolitical-shadow"

    def __init__(self, engine_factory=runtime_engine, capacity=64):
        super().__init__(engine_factory=engine_factory, capacity=capacity)

    def submit(self, event, *, make_current=True):
        # The full source text is never persisted (content hashes are); dropping it
        # before the snapshot copy keeps large official documents within bounds.
        if isinstance(event, dict) and "body" in event:
            event = {key: value for key, value in event.items() if key != "body"}
        return super().submit(event, make_current=make_current)

    def _persist(self, engine, event, observed_at, make_current):
        # Module-level lookup keeps the geopolitical persist function patchable in tests.
        return persist_geopolitical(engine, event, observed_at, make_current=make_current, report=True)


# Shared lifecycle; state stays in this module's namespace (see shadow_lifecycle).
_lifecycle = ShadowLifecycle(globals(), writer="GeopoliticalShadowWriter", label="Geopolitical")
submit_geopolitical = _lifecycle.submit
get_persistence_stats = _lifecycle.get_persistence_stats
shutdown = _lifecycle.shutdown
close_shadow = _lifecycle.close
_after_fork = _lifecycle.reset
