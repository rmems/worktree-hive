"""Tests for watchlist module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worktrees_hives.watchlist import JobState, JobStatus, Watchlist, _atomic_write_json


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a temporary state file path."""
    return tmp_path / "watchlist.json"


@pytest.fixture
def watchlist(state_path: Path) -> Watchlist:
    """Return a fresh Watchlist instance."""
    return Watchlist(state_path)


class TestAtomicWrite:
    """Tests for atomic JSON writing."""

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "state.json"
        _atomic_write_json(path, {"test": True})
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {"test": True}

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _atomic_write_json(path, {"v": 1})
        _atomic_write_json(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}

    def test_no_temp_files_left(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _atomic_write_json(path, {"test": True})
        tmp_files = list(tmp_path.glob(".watchlist-*"))
        assert len(tmp_files) == 0


class TestWatchlistAdd:
    """Tests for Watchlist.add."""

    def test_add_job(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "rmems", "repo", "feature/x")
        assert job.job_id == "j1"
        assert job.owner == "rmems"
        assert job.repo == "repo"
        assert job.branch == "feature/x"
        assert job.status == JobStatus.PENDING
        assert job.fix_count == 0
        assert job.max_fixes == 3

    def test_add_with_options(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "rmems", "repo", "br", stack_id="s1", max_fixes=3)
        assert job.stack_id == "s1"
        assert job.max_fixes == 3

    def test_add_negative_max_fixes_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(ValueError, match="max_fixes"):
            watchlist.add("j1", "rmems", "repo", "br", max_fixes=-1)

    def test_add_exceeds_safety_ceiling_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(ValueError, match="safety ceiling"):
            watchlist.add("j1", "rmems", "repo", "br", max_fixes=5)

    def test_add_duplicate_raises(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        with pytest.raises(ValueError, match="already exists"):
            watchlist.add("j1", "rmems", "repo", "br")

    def test_add_persists(self, state_path: Path) -> None:
        w1 = Watchlist(state_path)
        w1.add("j1", "rmems", "repo", "br")
        w2 = Watchlist(state_path)
        job = w2.get("j1")
        assert job is not None
        assert job.owner == "rmems"

    def test_corrupt_json_recovers_empty(self, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not valid json", encoding="utf-8")
        w = Watchlist(state_path)
        assert w.list_jobs() == []


class TestWatchlistRemove:
    """Tests for Watchlist.remove."""

    def test_remove_job(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        watchlist.remove("j1")
        assert watchlist.get("j1") is None

    def test_remove_nonexistent_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(KeyError, match="not found"):
            watchlist.remove("nope")


class TestWatchlistGet:
    """Tests for Watchlist.get."""

    def test_get_existing(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        job = watchlist.get("j1")
        assert job is not None
        assert job.job_id == "j1"

    def test_get_missing(self, watchlist: Watchlist) -> None:
        assert watchlist.get("nope") is None


class TestWatchlistList:
    """Tests for Watchlist.list_jobs."""

    def test_list_all(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "r1", "br")
        watchlist.add("j2", "rmems", "r2", "br")
        watchlist.add("j3", "Limen-Neural", "r3", "br")
        assert len(watchlist.list_jobs()) == 3

    def test_filter_by_owner(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "r1", "br")
        watchlist.add("j2", "Limen-Neural", "r2", "br")
        jobs = watchlist.list_jobs(owner="rmems")
        assert len(jobs) == 1
        assert jobs[0].owner == "rmems"

    def test_filter_by_repo(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "r1", "br")
        watchlist.add("j2", "rmems", "r2", "br")
        jobs = watchlist.list_jobs(repo="r1")
        assert len(jobs) == 1

    def test_filter_by_status(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "r1", "br")
        watchlist.add("j2", "rmems", "r2", "br")
        watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        jobs = watchlist.list_jobs(status=JobStatus.PENDING)
        assert len(jobs) == 1
        assert jobs[0].job_id == "j2"


class TestWatchlistUpdate:
    """Tests for Watchlist.update_status."""

    def test_update_status(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        job = watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        assert job.status == JobStatus.IN_PROGRESS

    def test_update_nonexistent_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(KeyError, match="not found"):
            watchlist.update_status("nope", JobStatus.COMPLETED)


class TestWatchlistFixCount:
    """Tests for Watchlist.increment_fix_count."""

    def test_increment(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        job = watchlist.increment_fix_count("j1")
        assert job.fix_count == 1
        assert job.fix_budget_remaining == 2

    def test_exhaust_budget_raises(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br", max_fixes=1)
        watchlist.increment_fix_count("j1")
        with pytest.raises(ValueError, match="exhausted"):
            watchlist.increment_fix_count("j1")


class TestWatchlistBlockers:
    """Tests for Watchlist.set_blockers."""

    def test_set_blockers(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        job = watchlist.set_blockers("j1", ["ci failing", "merge conflict"])
        assert job.residual_blockers == ["ci failing", "merge conflict"]

    def test_clear_blockers(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        watchlist.set_blockers("j1", ["blocker"])
        job = watchlist.set_blockers("j1", [])
        assert job.residual_blockers == []


class TestWatchlistPR:
    """Tests for Watchlist.set_pr."""

    def test_set_pr(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "repo", "br")
        job = watchlist.set_pr("j1", 42, "https://github.com/rmems/repo/pull/42")
        assert job.pr_number == 42
        assert job.pr_url == "https://github.com/rmems/repo/pull/42"


class TestWatchlistCheck:
    """Tests for Watchlist.check."""

    def test_empty(self, watchlist: Watchlist) -> None:
        result = watchlist.check()
        assert all(len(v) == 0 for v in result.values())

    def test_categorize(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "r1", "br")
        watchlist.add("j2", "rmems", "r2", "br")
        watchlist.add("j3", "rmems", "r3", "br")
        watchlist.add("j4", "rmems", "r4", "br")
        watchlist.set_pr("j2", 1, "https://example.com/pr/1")
        watchlist.set_blockers("j3", ["failing test"])
        watchlist.update_status("j4", JobStatus.COMPLETED)

        result = watchlist.check()
        assert len(result["needs_pr"]) == 1
        assert result["needs_pr"][0].job_id == "j1"
        assert len(result["needs_fix"]) == 1
        assert result["needs_fix"][0].job_id == "j2"
        assert len(result["blocked"]) == 1
        assert result["blocked"][0].job_id == "j3"
        assert len(result["done"]) == 1
        assert result["done"][0].job_id == "j4"


class TestJobState:
    """Tests for JobState properties."""

    def test_full_repo(self) -> None:
        job = JobState("j1", "rmems", "repo", "br")
        assert job.full_repo == "rmems/repo"

    def test_is_actionable(self) -> None:
        job = JobState("j1", "rmems", "repo", "br")
        assert job.is_actionable is True
        job.status = JobStatus.COMPLETED
        assert job.is_actionable is False

    def test_fix_budget(self) -> None:
        job = JobState("j1", "rmems", "repo", "br", fix_count=2, max_fixes=3)
        assert job.fix_budget_remaining == 1
        job.fix_count = 3
        assert job.fix_budget_remaining == 0


class TestMultiOwner:
    """Tests for multi-owner repo support."""

    def test_rmems_owner(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "rmems", "worktrees-hives", "feature/x")
        assert job.full_repo == "rmems/worktrees-hives"

    def test_limen_neural_owner(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j2", "Limen-Neural", "project", "main")
        assert job.full_repo == "Limen-Neural/project"

    def test_filter_across_owners(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "rmems", "r1", "br")
        watchlist.add("j2", "Limen-Neural", "r2", "br")
        watchlist.add("j3", "rmems", "r3", "br")
        rmems_jobs = watchlist.list_jobs(owner="rmems")
        ln_jobs = watchlist.list_jobs(owner="Limen-Neural")
        assert len(rmems_jobs) == 2
        assert len(ln_jobs) == 1
