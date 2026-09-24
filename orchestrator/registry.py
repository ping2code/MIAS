"""Deterministic collector registry: name -> existing entry point (argument lists only; no collector imports).

| Collector    | Command                                              | Alerts / AI (unchanged entry-point defaults)       |
|--------------|------------------------------------------------------|----------------------------------------------------|
| news         | python -m collector.multi_source_collector           | RSS sources; delivers ALERT items, AI for ALERT    |
| fed          | python -m collector.fed_collector [--send-alerts]    | AI on; alerts only with --send-alerts              |
| sec          | python -m collector.sec_collector                    | delivers ALERT filings; no AI path                 |
| macro        | python -m collector.macro_collector [--send-alerts]  | AI on; alerts only with --send-alerts              |
| treasury     | python -m collector.treasury_collector [--send-alerts] | AI on; alerts only with --send-alerts            |
| geopolitical | python -m orchestrator.entrypoints geopolitical [--send-alerts] | wrapper with Fed-style flags/defaults   |
| technical    | python -m technical.runner                           | no alerts, no AI; disabled by default (Phase 4C)   |
"""
import sys

from orchestrator.config import FAMILIES, SEND_ALERTS_FLAG
from orchestrator.models import JobDefinition, OverlapPolicy

MODULES = dict(news="collector.multi_source_collector", fed="collector.fed_collector", sec="collector.sec_collector",
               macro="collector.macro_collector", treasury="collector.treasury_collector",
               geopolitical="orchestrator.entrypoints", technical="technical.runner")
ENTRY_ARGS = dict(geopolitical=("geopolitical",))


def command(name, *, send_alerts=False, python=None):
    """The child argument list for one collector (never a shell string; never secrets)."""
    argv = [python or sys.executable, "-m", MODULES[name], *ENTRY_ARGS.get(name, ())]
    if send_alerts and name in SEND_ALERTS_FLAG:
        argv.append("--send-alerts")
    return tuple(argv)


def build_definitions(settings, *, python=None):
    """Job definitions in registry order (news, fed, sec, macro, treasury, geopolitical, technical)."""
    by_name = {f.name: f for f in settings.families}
    return [JobDefinition(name=name, argv=command(name, send_alerts=by_name[name].send_alerts, python=python),
                          enabled=by_name[name].enabled, interval_seconds=by_name[name].interval_seconds,
                          timeout_seconds=by_name[name].timeout_seconds,
                          start_offset_seconds=by_name[name].start_offset_seconds, overlap_policy=OverlapPolicy.SKIP,
                          kill_grace_seconds=settings.kill_grace_seconds)
            for name in FAMILIES]
