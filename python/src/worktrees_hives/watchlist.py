"""Watchlist: persistent multi-job hive state across check cycles."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    """Status of a watched job."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class PolicyError(ValueError):
    """Safety-policy rejection (maps to CLI exit code 2)."""


@dataclass
class JobState:
    """State of a single job in the watchlist."""

    job_id: str
    owner: str
    repo: str
    branch: str
    status: JobStatus = JobStatus.PENDING
    stack_id: str | None = None
    fix_count: int = 0
    max_fixes: int = 3
    residual_blockers: list[str] = field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    last_check: str | None = None
    error: str | None = None

    @property
    def full_repo(self) -> str:
        """Return owner/repo format."""
        return f"{self.owner}/{self.repo}"

    @property
    def is_actionable(self) -> bool:
        """Return True if the job can accept new work."""
        return self.status in {
            JobStatus.PENDING,
            JobStatus.IN_PROGRESS,
            JobStatus.BLOCKED,
        }

    @property
    def fix_budget_remaining(self) -> int:
        """Return remaining fix commits allowed."""
        return max(0, self.max_fixes - self.fix_count)


def _default_state_path() -> Path:
    """Return the default watchlist state file path under XDG data home."""
    xdg_data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(xdg_data) / "worktrees-hives" / "watchlist.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".watchlist-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    """Read JSON file, returning empty dict on missing or corrupt file."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result
    except json.JSONDecodeError:
        return {}


class Watchlist:
    """Persistent watchlist for multi-job hive state.

    State is stored as JSON at a configurable path (default: XDG data home).
    Writes are atomic (temp file + rename).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_state_path()
        self._jobs: dict[str, JobState] = {}
        self._load()

    @property
    def path(self) -> Path:
        """Return the state file path."""
        return self._path

    def _load(self) -> None:
        """Load state from disk."""
        data = _read_json(self._path)
        jobs_data = data.get("jobs", {})
        self._jobs = {}
        for job_id, job_dict in jobs_data.items():
            try:
                d = dict(job_dict)
                d["status"] = JobStatus(d["status"])
                self._jobs[job_id] = JobState(**d)
            except (KeyError, ValueError, TypeError):
                continue  # skip corrupt/incompatible entry

    def _save(self) -> None:
        """Save state to disk atomically."""
        data = {
            "schema_version": 1,
            "jobs": {jid: asdict(j) for jid, j in self._jobs.items()},
        }
        _atomic_write_json(self._path, data)

    def add(
        self,
        job_id: str,
        owner: str,
        repo: str,
        branch: str,
        stack_id: str | None = None,
        max_fixes: int = 3,
    ) -> JobState:
        """Add a new job to the watchlist.

        Raises ValueError if job_id already exists or max_fixes is negative.
        Raises PolicyError if max_fixes exceeds the safety ceiling (3).
        """
        if max_fixes < 0:
            raise ValueError("max_fixes must be non-negative")
        if max_fixes > 3:
            raise PolicyError(
                f"max_fixes ({max_fixes}) exceeds safety ceiling (3). "
                "Per AGENTS.md, at most 3 code-fix commits per PR per cycle."
            )
        if job_id in self._jobs:
            raise ValueError(f"Job {job_id!r} already exists in watchlist")
        job = JobState(
            job_id=job_id,
            owner=owner,
            repo=repo,
            branch=branch,
            stack_id=stack_id,
            max_fixes=max_fixes,
        )
        self._jobs[job_id] = job
        self._save()
        return job

    def remove(self, job_id: str) -> None:
        """Remove a job from the watchlist.

        Raises KeyError if job_id not found.
        """
        if job_id not in self._jobs:
            raise KeyError(f"Job {job_id!r} not found in watchlist")
        del self._jobs[job_id]
        self._save()

    def get(self, job_id: str) -> JobState | None:
        """Get a job by ID, or None if not found."""
        return self._jobs.get(job_id)

    def list_jobs(
        self,
        owner: str | None = None,
        repo: str | None = None,
        status: JobStatus | None = None,
    ) -> list[JobState]:
        """List jobs with optional filters."""
        result = list(self._jobs.values())
        if owner is not None:
            result = [j for j in result if j.owner == owner]
        if repo is not None:
            result = [j for j in result if j.repo == repo]
        if status is not None:
            result = [j for j in result if j.status == status]
        return result

    def update_status(self, job_id: str, status: JobStatus) -> JobState:
        """Update a job's status.

        Raises KeyError if job_id not found.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id!r} not found in watchlist")
        job.status = status
        self._save()
        return job

    def increment_fix_count(self, job_id: str) -> JobState:
        """Increment the fix count for a job.

        Raises KeyError if job_id not found.
        Raises PolicyError if fix budget exhausted.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id!r} not found in watchlist")
        if job.fix_count >= job.max_fixes:
            raise PolicyError(f"Job {job_id!r} has exhausted its fix budget ({job.max_fixes})")
        job.fix_count += 1
        self._save()
        return job

    def set_blockers(self, job_id: str, blockers: list[str]) -> JobState:
        """Set residual blockers for a job.

        Raises KeyError if job_id not found.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id!r} not found in watchlist")
        job.residual_blockers = list(blockers)
        self._save()
        return job

    def set_pr(self, job_id: str, pr_number: int, pr_url: str) -> JobState:
        """Set PR info for a job.

        Raises KeyError if job_id not found.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id!r} not found in watchlist")
        job.pr_number = pr_number
        job.pr_url = pr_url
        self._save()
        return job

    def check(self) -> dict[str, list[JobState]]:
        """Check all jobs and categorize by action needed.

        Returns dict with keys: needs_pr, needs_fix, blocked, ready, done.
        """
        result: dict[str, list[JobState]] = {
            "needs_pr": [],
            "needs_fix": [],
            "blocked": [],
            "ready": [],
            "done": [],
        }
        for job in self._jobs.values():
            if job.status == JobStatus.COMPLETED or job.status == JobStatus.FAILED:
                result["done"].append(job)
            elif job.residual_blockers:
                result["blocked"].append(job)
            elif job.pr_number is None:
                result["needs_pr"].append(job)
            elif job.fix_budget_remaining > 0:
                result["needs_fix"].append(job)
            else:
                result["ready"].append(job)
        return result
