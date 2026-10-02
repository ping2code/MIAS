"""Readiness model: named checks that pass or fail.

A check is a ``ReadinessCheck(name, probe)``; ``probe()`` returns True when ready. A probe that returns anything else
or raises counts as a failure, and the exception is never reported. The report holds only check names and
``pass``/``fail``: never hosts, URLs, tokens, paths, file names or validation text.

Default checks:
- ``settings``: the app only exists with validated settings;
- ``artifact_root``: ``MIAS_ARTIFACT_ROOT`` is configured and is a real (non-symlink) directory the process can open;
- ``artifact_index``: the artifact index has been built and the latest refresh succeeded (a corrupt or unexpected
  artifact fails it);
- ``receipt_root`` (only when ``MIAS_RECEIPT_ROOT`` is set): the directory exists and its whole Phase 12E receipt
  history validates.

Provider APIs, Telegram, Redis and PostgreSQL are not readiness dependencies.
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


def root_probe(path):
    def probe():
        from artifact_store.files import open_root
        with open_root(path):
            return True
    return probe


def receipt_probe(path):
    def probe():
        from alert_engine.receipts import read_receipts
        if not os.path.isdir(path):
            return False
        read_receipts(path)
        return True
    return probe


def default_checks(settings, artifact_store=None):
    root = settings.artifact_root
    checks = [ReadinessCheck("settings", lambda: True),
              ReadinessCheck("artifact_root", root_probe(root) if root is not None else (lambda: False)),
              ReadinessCheck("artifact_index", lambda: artifact_store is not None and artifact_store.healthy)]
    if settings.receipt_root is not None:
        checks.append(ReadinessCheck("receipt_root", receipt_probe(settings.receipt_root)))
    return tuple(checks)
