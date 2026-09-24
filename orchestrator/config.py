"""Scheduler settings from the process environment only (never ``.env``); validated with safe defaults.

All values below are conservative **staging examples**, not tuned production cadence:
production polling intervals remain a separate decision.
"""
from dataclasses import dataclass

FAMILIES = ("news", "fed", "sec", "macro", "treasury", "geopolitical")
# (interval seconds, timeout seconds, start offset seconds): deterministic offsets avoid a start-up stampede.
DEFAULTS = dict(news=(300, 240, 0), fed=(600, 300, 5), sec=(900, 300, 10),
                macro=(1800, 900, 15), treasury=(1800, 900, 20), geopolitical=(1800, 1500, 25))
PREFIX = dict(news="NEWS", fed="FED", sec="SEC", macro="MACRO", treasury="TREASURY", geopolitical="GEOPOLITICAL")
# Families whose existing entry points take ``--send-alerts`` (opt-in; SEC/News entry points always deliver today).
SEND_ALERTS_FLAG = ("fed", "macro", "treasury", "geopolitical")
MIN_INTERVAL, MAX_INTERVAL, MAX_OFFSET = 60, 86_400, 3_600


class SchedulerConfigError(ValueError):
    """Invalid scheduler configuration; messages name the setting, never other environment values."""


def _bool(environ, name, default):
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value not in ("true", "false"):
        raise SchedulerConfigError(f"{name} must be true or false")
    return value == "true"


def _int(environ, name, default, low, high):
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SchedulerConfigError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise SchedulerConfigError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class FamilySettings:
    name: str
    enabled: bool
    interval_seconds: int
    timeout_seconds: int
    start_offset_seconds: int
    send_alerts: bool


@dataclass(frozen=True)
class SchedulerSettings:
    enabled: bool
    dry_run: bool
    history_size: int
    shutdown_grace_seconds: int
    kill_grace_seconds: int
    child_output: str
    families: tuple

    def safe_view(self):
        """Settings safe to print: no environment values other than these validated scheduler settings."""
        return dict(enabled=self.enabled, dry_run=self.dry_run, history_size=self.history_size,
                    shutdown_grace_seconds=self.shutdown_grace_seconds, kill_grace_seconds=self.kill_grace_seconds,
                    child_output=self.child_output,
                    families={f.name: dict(enabled=f.enabled, interval_seconds=f.interval_seconds,
                                           timeout_seconds=f.timeout_seconds, start_offset_seconds=f.start_offset_seconds,
                                           send_alerts=f.send_alerts if f.name in SEND_ALERTS_FLAG
                                           else "always (entry point delivers ALERT items)")
                              for f in self.families})


def load_settings(environ):
    families = []
    for name in FAMILIES:
        prefix = PREFIX[name]
        interval_default, timeout_default, offset_default = DEFAULTS[name]
        interval = _int(environ, f"{prefix}_INTERVAL_SECONDS", interval_default, MIN_INTERVAL, MAX_INTERVAL)
        timeout = _int(environ, f"{prefix}_TIMEOUT_SECONDS", min(timeout_default, interval - 1), 1, MAX_INTERVAL)
        if timeout >= interval:
            raise SchedulerConfigError(f"{prefix}_TIMEOUT_SECONDS must be below {prefix}_INTERVAL_SECONDS")
        families.append(FamilySettings(
            name=name, enabled=_bool(environ, f"{prefix}_SCHEDULE_ENABLED", True), interval_seconds=interval,
            timeout_seconds=timeout,
            start_offset_seconds=_int(environ, f"{prefix}_START_OFFSET_SECONDS", offset_default, 0, MAX_OFFSET),
            send_alerts=_bool(environ, f"{prefix}_SCHEDULE_SEND_ALERTS", False) if name in SEND_ALERTS_FLAG else False))
        if name not in SEND_ALERTS_FLAG and environ.get(f"{prefix}_SCHEDULE_SEND_ALERTS"):
            raise SchedulerConfigError(f"{prefix}_SCHEDULE_SEND_ALERTS is not supported: its entry point has no such flag")
    output = (environ.get("MIAS_SCHEDULER_CHILD_OUTPUT") or "inherit").strip().lower()
    if output not in ("inherit", "discard"):
        raise SchedulerConfigError("MIAS_SCHEDULER_CHILD_OUTPUT must be inherit or discard")
    return SchedulerSettings(
        enabled=_bool(environ, "MIAS_SCHEDULER_ENABLED", False), dry_run=_bool(environ, "MIAS_SCHEDULER_DRY_RUN", False),
        history_size=_int(environ, "MIAS_SCHEDULER_HISTORY_SIZE", 50, 1, 1000),
        shutdown_grace_seconds=_int(environ, "MIAS_SCHEDULER_SHUTDOWN_GRACE_SECONDS", 30, 0, 600),
        kill_grace_seconds=_int(environ, "MIAS_SCHEDULER_KILL_GRACE_SECONDS", 10, 1, 120),
        child_output=output, families=tuple(families))
