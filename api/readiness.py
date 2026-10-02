"""Readiness model: named checks that pass or fail. Phase 13C adds the artifact-store checks here.

A check is a ``ReadinessCheck(name, probe)``; ``probe()`` returns True when ready. A probe that returns anything else
or raises counts as a failure, and the exception is never reported. The report holds only check names and
``pass``/``fail``: never hosts, URLs, tokens, paths or file contents.

Checks in this version: ``settings`` (the app only exists with validated settings) and, when configured,
``artifact_root`` and ``receipt_root`` (an existing directory the process can list; nothing is read). Provider APIs,
Telegram, Redis and PostgreSQL are not readiness dependencies.
"""
from dataclasses import dataclass
import os
import re
from typing import Callable

CHECK_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
PASS, FAIL = "pass", "fail"


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    probe: Callable[[], bool]

    def __post_init__(self):
        if not (isinstance(self.name, str) and CHECK_NAME.fullmatch(self.name) and callable(self.probe)):
            raise ValueError("readiness check needs a lowercase name and a callable probe")


def run_checks(checks):
    """[(name, pass|fail)] in the given order."""
    results = []
    for check in checks:
        try:
            ok = check.probe() is True
        except Exception:
            ok = False
        results.append((check.name, PASS if ok else FAIL))
    return results


def directory_probe(path):
    return lambda: os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def default_checks(settings):
    checks = [ReadinessCheck("settings", lambda: True)]
    if settings.artifact_root is not None:
        checks.append(ReadinessCheck("artifact_root", directory_probe(settings.artifact_root)))
    if settings.receipt_root is not None:
        checks.append(ReadinessCheck("receipt_root", directory_probe(settings.receipt_root)))
    return tuple(checks)
