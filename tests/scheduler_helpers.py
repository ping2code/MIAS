"""Shared helpers for Phase 3 scheduler tests (fixture child processes; no collectors, no network)."""
import json
import os
import sys
import time
from pathlib import Path

from orchestrator.models import JobDefinition

WORKER = (sys.executable, "-m", "tests.scheduler_fixture_worker")


def fixture(name, *args, interval=1.0, timeout=5.0, offset=0.0, kill_grace=1.0, enabled=True):
    return JobDefinition(name=name, argv=WORKER + tuple(str(a) for a in args), enabled=enabled, interval_seconds=interval,
                         timeout_seconds=timeout, start_offset_seconds=offset, kill_grace_seconds=kill_grace)


def markers(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def alive(pid):
    """True while the pid exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/status") as status:
            return not any(line.startswith("State:") and "Z" in line.split()[1] for line in status)
    except FileNotFoundError:
        return False


def wait_dead(pids, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return not any(alive(pid) for pid in pids)


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
