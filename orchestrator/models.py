"""Scheduler data model: job definitions, run states and structured results (no collector logic)."""
from dataclasses import asdict, dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED_OVERLAP = "skipped_overlap"
    DISABLED = "disabled"
    DRY_RUN = "dry_run"        # Would have run (dry-run mode); nothing executed.
    CANCELLED = "cancelled"    # Terminated by scheduler shutdown after the bounded grace period.


class OverlapPolicy(str, Enum):
    SKIP = "skip"  # The only Phase 3 policy: a collector never overlaps with itself.


@dataclass(frozen=True)
class JobDefinition:
    """What the scheduler knows about a collector: never its business logic."""
    name: str
    argv: tuple
    enabled: bool = True
    interval_seconds: float = 300.0
    timeout_seconds: float = 240.0
    start_offset_seconds: float = 0.0
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    kill_grace_seconds: float = 10.0

    def __post_init__(self):
        if not self.name or not self.argv or not all(isinstance(a, str) for a in self.argv):
            raise ValueError("Job definition needs a name and an argument list of strings")
        if self.interval_seconds <= 0 or self.timeout_seconds <= 0 or self.kill_grace_seconds <= 0:
            raise ValueError("Interval, timeout and kill grace must be positive")
        if self.start_offset_seconds < 0:
            raise ValueError("Start offset must not be negative")
        if self.overlap_policy is not OverlapPolicy.SKIP:
            raise ValueError("Only the skip overlap policy is supported")


@dataclass
class JobResult:
    """Structured run metadata only: never collector stdout/stderr or environment."""
    collector: str
    run_id: str
    status: JobStatus
    scheduled_at: str = None
    started_at: str = None
    finished_at: str = None
    duration_seconds: float = None
    exit_code: int = None
    error_summary: str = None
    pid: int = field(default=None, repr=False)

    def to_dict(self):
        data = asdict(self)
        data["status"] = self.status.value
        data.pop("pid")
        return data
