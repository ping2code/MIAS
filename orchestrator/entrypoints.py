"""Minimal script entry point for collectors that lack one (geopolitical), mirroring the Fed CLI.

    python -m orchestrator.entrypoints geopolitical [--no-ai] [--send-alerts]

It only parses the same two flags as ``collector.fed_collector`` and calls the unchanged
``collect_geopolitical_events``; no processing logic lives here. Like the Fed entry point it
exits 0 after a completed cycle and non-zero only if the collector raises.
"""
import argparse
import json
import sys


def geopolitical(argv):
    parser = argparse.ArgumentParser(prog="python -m orchestrator.entrypoints geopolitical")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--send-alerts", action="store_true")
    args = parser.parse_args(argv)
    from collector.geopolitical_collector import collect_geopolitical_events, format_geopolitical_alert
    events, stats = collect_geopolitical_events(enable_ai=not args.no_ai, send_alerts=args.send_alerts)
    print(json.dumps(stats, indent=2))
    for event in events:
        print(format_geopolitical_alert(event))
    return 0


ENTRYPOINTS = dict(geopolitical=geopolitical)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ENTRYPOINTS:
        print(f"usage: python -m orchestrator.entrypoints {{{','.join(ENTRYPOINTS)}}} [options]", file=sys.stderr)
        return 2
    return ENTRYPOINTS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
