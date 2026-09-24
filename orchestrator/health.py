"""Internal scheduler health snapshot (plain Python data; no HTTP API in Phase 3)."""
from datetime import timedelta


def snapshot(scheduler):
    """Running state, per-collector last/next run and recent status counts; safe to log (no secrets, no output)."""
    now_mono, now_wall = scheduler.clock(), scheduler.wall()
    collectors = {}
    for definition in scheduler.definitions:
        name = definition.name
        history = list(scheduler.history[name])
        last = history[-1] if history else None
        due = scheduler.next_due.get(name)
        counts = {}
        for result in history:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        collectors[name] = dict(
            enabled=definition.enabled, interval_seconds=definition.interval_seconds,
            timeout_seconds=definition.timeout_seconds, currently_running=name in scheduler.running,
            last_status=last.status.value if last else None,
            last_run_at=(last.started_at or last.scheduled_at) if last else None,
            next_run_at=(now_wall + timedelta(seconds=max(0.0, due - now_mono))).isoformat(timespec="seconds")
            if definition.enabled and due is not None and not scheduler.stopping else None,
            recent_status_counts=counts, history_size=len(history))
    return dict(running=scheduler.started_at is not None and not scheduler.stopped, stopping=scheduler.stopping,
                dry_run=scheduler.dry_run, started_at=scheduler.started_at.isoformat(timespec="seconds")
                if scheduler.started_at else None, active_runs=len(scheduler.running), collectors=collectors)
