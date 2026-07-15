"""Watchlist: persistent multi-job hive state across check cycles."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

# Documented state filename (AGENTS.md / Rust contract). Override with WH_STATE_PATH.
STATE_FILENAME = "watched.json"
WH_STATE_PATH_ENV = "WH_STATE_PATH"
WH_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"
MAX_FIXES_CEILING = 3

# Empty default — no org hardcoding. Configure via WH_ALLOWED_OWNERS or
# Watchlist(allowed_owners=...). Empty allowlist = allow any owner.
ALLOWED_OWNERS: frozenset[str] = frozenset()


def load_allowed_owners_from_env() -> frozenset[str]:
    """Parse WH_ALLOWED_OWNERS (comma-separated). Empty/unset → empty set."""
    raw = os.environ.get(WH_ALLOWED_OWNERS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class JobStatus(str, Enum):
    """Status of a watched job."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class PolicyError(ValueError):
    """Safety-policy rejection (maps to CLI exit code 2)."""


class CorruptStateError(ValueError):
    """State file is corrupt; original was quarantined. Do not silently wipe."""


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
        return max(0, min(self.max_fixes, MAX_FIXES_CEILING) - self.fix_count)


# Known JobState field names for additive-v1 compatibility (ignore extras on construct).
_JOB_STATE_FIELDS: frozenset[str] = frozenset(f.name for f in fields(JobState))


def _platform_data_dir() -> Path:
    """Return a platform-aware user data directory for worktrees-hives."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "worktrees-hives"
        return Path.home() / "AppData" / "Local" / "worktrees-hives"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "worktrees-hives"
    # Linux / other Unix: XDG
    xdg_data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(xdg_data) / "worktrees-hives"


def _default_state_path() -> Path:
    """Return the default state file path.

    Honors WH_STATE_PATH when set; otherwise uses platform data dir + watched.json.
    """
    env_path = os.environ.get(WH_STATE_PATH_ENV)
    if env_path:
        return Path(env_path)
    return _platform_data_dir() / STATE_FILENAME


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".watched-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _quarantine_corrupt(path: Path) -> Path:
    """Move a corrupt state file aside; return quarantine path."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    quarantine = path.with_name(f"{path.name}.corrupt.{stamp}")
    # Avoid clobbering an existing quarantine path
    n = 0
    candidate = quarantine
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.corrupt.{stamp}.{n}")
    path.replace(candidate)
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    """Read JSON state file.

    Missing file → empty dict (first run).
    Corrupt JSON → quarantine the file and raise CorruptStateError (never silent wipe).
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            if not isinstance(result, dict):
                raise json.JSONDecodeError(
                    "state root must be a JSON object",
                    doc=str(result),
                    pos=0,
                )
            return result
    except json.JSONDecodeError as e:
        quarantine = _quarantine_corrupt(path)
        raise CorruptStateError(
            f"Corrupt watchlist state at {path}; quarantined to {quarantine}: {e}. "
            "Refusing to load empty state that would wipe durable data on next save."
        ) from e


def _validate_max_fixes(max_fixes: int) -> int:
    """Validate max_fixes is in [0, MAX_FIXES_CEILING]."""
    if max_fixes < 0:
        raise ValueError("max_fixes must be non-negative")
    if max_fixes > MAX_FIXES_CEILING:
        raise PolicyError(
            f"max_fixes ({max_fixes}) exceeds safety ceiling ({MAX_FIXES_CEILING}). "
            "Per AGENTS.md, at most 3 code-fix commits per PR per cycle."
        )
    return max_fixes


def _owner_allowed(owner: str, allowed_owners: frozenset[str]) -> bool:
    """Return whether owner is within the configured repository scope."""
    return not allowed_owners or owner in allowed_owners


class Watchlist:
    """Persistent watchlist for multi-job hive state.

    State is stored as JSON at a configurable path (default: platform data dir /
    watched.json, or WH_STATE_PATH). Writes are atomic (temp file + rename).
    """

    def __init__(
        self,
        path: Path | None = None,
        allowed_owners: frozenset[str] | None = None,
    ) -> None:
        self._path = path or _default_state_path()
        self._jobs: dict[str, JobState] = {}
        # Additive v1 fields (kind, worktree_path, timestamps, …) preserved per job.
        self._job_extras: dict[str, dict[str, Any]] = {}
        if allowed_owners is None:
            self._allowed_owners = load_allowed_owners_from_env()
        else:
            self._allowed_owners = frozenset(allowed_owners)
        self._load()

    @property
    def path(self) -> Path:
        """Return the state file path."""
        return self._path

    def _load(self) -> None:
        """Load state from disk; re-validate max_fixes on load.

        Unknown keys on job objects are treated as additive v1 fields: filtered
        out of JobState construction and preserved for round-trip on save so a
        compatible writer (e.g. Rust) does not lose watched jobs.
        """
        data = _read_json(self._path)
        jobs_data = data.get("jobs", {})
        if not isinstance(jobs_data, dict):
            jobs_data = {}
        self._jobs = {}
        self._job_extras = {}
        for job_id, job_dict in jobs_data.items():
            try:
                if not isinstance(job_dict, dict):
                    continue
                raw = dict(job_dict)
                extras = {k: v for k, v in raw.items() if k not in _JOB_STATE_FIELDS}
                d = {k: v for k, v in raw.items() if k in _JOB_STATE_FIELDS}
                d["status"] = JobStatus(d["status"])
                max_fixes = int(d.get("max_fixes", 3))
                _validate_max_fixes(max_fixes)
                d["max_fixes"] = max_fixes
                fix_count = int(d.get("fix_count", 0))
                if fix_count < 0:
                    continue
                d["fix_count"] = fix_count
                owner = str(d["owner"])
                if not _owner_allowed(owner, self._allowed_owners):
                    continue
                d["owner"] = owner
                jid = str(job_id)
                self._jobs[jid] = JobState(**d)
                if extras:
                    self._job_extras[jid] = extras
            except (KeyError, ValueError, TypeError, PolicyError):
                continue  # skip corrupt/incompatible entry

    def _save(self) -> None:
        """Save state to disk atomically."""
        jobs: dict[str, dict[str, Any]] = {}
        for jid, job in self._jobs.items():
            job_dict: dict[str, Any] = asdict(job)
            # Enum values → strings for JSON
            status = job_dict.get("status")
            if isinstance(status, JobStatus):
                job_dict["status"] = status.value
            # Preserve additive v1 fields from compatible writers
            for key, value in self._job_extras.get(jid, {}).items():
                if key not in job_dict:
                    job_dict[key] = value
            jobs[jid] = job_dict
        data: dict[str, Any] = {
            "schema_version": 1,
            "jobs": jobs,
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
        Raises PolicyError if max_fixes exceeds the safety ceiling (3) or
        owner is outside the configured allowlist.
        """
        max_fixes = _validate_max_fixes(max_fixes)
        if not _owner_allowed(owner, self._allowed_owners):
            raise PolicyError(
                f"Owner {owner!r} not in allowed owners: "
                f"{sorted(self._allowed_owners)}. "
                f"Set {WH_ALLOWED_OWNERS_ENV} or pass allowed_owners=."
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
        self._job_extras.pop(job_id, None)
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

        Enforces both the per-job max_fixes budget and the global safety
        ceiling (MAX_FIXES_CEILING) so a misconfigured max cannot exceed policy.

        Raises KeyError if job_id not found.
        Raises PolicyError if fix budget or safety ceiling is exhausted.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id!r} not found in watchlist")
        effective_max = min(job.max_fixes, MAX_FIXES_CEILING)
        if job.fix_count >= effective_max:
            raise PolicyError(
                f"Job {job_id!r} has exhausted its fix budget "
                f"({effective_max}; ceiling {MAX_FIXES_CEILING})"
            )
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

        Categories:
          - needs_pr: actionable job with no PR yet
          - needs_fix: has residual blockers and remaining fix budget
          - blocked: residual blockers with exhausted budget, or explicit BLOCKED status
          - ready: has PR, no residual blockers (awaiting merge / healthy)
          - done: COMPLETED or FAILED

        Note: a green PR with remaining budget is **ready**, not needs_fix.
        Exhausted budget with blockers is **blocked**, not ready.
        """
        result: dict[str, list[JobState]] = {
            "needs_pr": [],
            "needs_fix": [],
            "blocked": [],
            "ready": [],
            "done": [],
        }
        for job in self._jobs.values():
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                result["done"].append(job)
            elif job.residual_blockers:
                if job.fix_budget_remaining > 0 and job.status != JobStatus.BLOCKED:
                    result["needs_fix"].append(job)
                else:
                    result["blocked"].append(job)
            elif job.status == JobStatus.BLOCKED:
                result["blocked"].append(job)
            elif job.pr_number is None:
                result["needs_pr"].append(job)
            else:
                # Has PR, no residual blockers — ready regardless of remaining budget
                result["ready"].append(job)
        return result
