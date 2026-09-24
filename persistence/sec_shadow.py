"""Best-effort bounded SEC shadow writes; reuses the shared writer contract.

Never touches Redis: the ``sec:event`` dedup keys remain owned by the collector.
"""
from persistence import macro_shadow
from persistence.adapters.sec import adapt_sec, sec_promotion
from persistence.database import transaction
from persistence.repository import EventRepository
from persistence.shadow_lifecycle import ShadowLifecycle
from shared.logger import get_logger

logger = get_logger("sec_shadow")


def persist_sec(engine, event, observed_at, *, make_current=True, report=False):
    """One atomic transaction: facts, evidence, then existing outcome snapshots."""
    adapted = adapt_sec(event, observed_at, make_current=make_current)
    with transaction(engine) as session:
        repo = EventRepository(session)
        row = repo.record(**adapted["record"], promotion_policy=sec_promotion)
        for key, values in adapted["provenance"]:
            repo.add_provenance(row["id"], key, **values)
        for kind, values in adapted["histories"]:
            repo.append_history(row["id"], kind, values)
    return {"version": row, "duplicate": repo.inserted_count == 0,
            "promotion": repo.promotion_reason} if report else row


def runtime_engine():
    return macro_shadow.runtime_engine(application_name="mias_sec_shadow")


_MESSAGES = {key: value.replace("Macro shadow", "SEC shadow") for key, value in macro_shadow._MESSAGES.items()}


class SecShadowWriter(macro_shadow.ShadowWriter):
    """Same queue, counters, logging bounds and shutdown semantics as macro."""
    logger = logger
    messages = _MESSAGES
    thread_name = "mias-sec-shadow"

    def __init__(self, engine_factory=runtime_engine, capacity=64):
        super().__init__(engine_factory=engine_factory, capacity=capacity)

    def _persist(self, engine, event, observed_at, make_current):
        # Module-level lookup keeps the SEC persist function patchable in tests.
        return persist_sec(engine, event, observed_at, make_current=make_current, report=True)


# Shared lifecycle; state stays in this module's namespace (see shadow_lifecycle).
_lifecycle = ShadowLifecycle(globals(), writer="SecShadowWriter", label="SEC")
submit_sec = _lifecycle.submit
get_persistence_stats = _lifecycle.get_persistence_stats
shutdown = _lifecycle.shutdown
close_shadow = _lifecycle.close
_after_fork = _lifecycle.reset
