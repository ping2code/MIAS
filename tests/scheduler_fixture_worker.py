"""Dummy child process for scheduler tests (never a collector; no network, no MIAS imports).

    python -m tests.scheduler_fixture_worker [--sleep S] [--exit N] [--ignore-term]
                                             [--marker FILE] [--grandchild] [--env-out FILE]

Appends JSON lines to ``--marker``: {"event": "start"|"end"|"grandchild", "pid": ..., "t": monotonic}.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time


def mark(path, event, **extra):
    if path:
        with open(path, "a") as handle:
            handle.write(json.dumps(dict(event=event, pid=os.getpid(), t=time.monotonic(), **extra)) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--marker")
    parser.add_argument("--grandchild", action="store_true")
    parser.add_argument("--env-out")
    args = parser.parse_args()
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    mark(args.marker, "start")
    if args.env_out:
        with open(args.env_out, "w") as handle:
            json.dump(dict(keys=sorted(os.environ), pythonpath=os.environ.get("PYTHONPATH")), handle)
    if args.grandchild:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        mark(args.marker, "grandchild", child=child.pid)
    time.sleep(args.sleep)
    mark(args.marker, "end")
    return args.exit


if __name__ == "__main__":
    sys.exit(main())
