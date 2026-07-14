"""Tests for watchlist module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worktrees_hives.watchlist import (
    ALLOWED_OWNERS,
    CorruptStateError,
    JobState,
    JobStatus,
    PolicyError,
    Watchlist,
    _atomic_write_json,
    _default_state_path,
    load_allowed_owners_from_env,
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a temporary state file path (watched.json)."""
    return tmp_path / "watched.json"


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
        tmp_files = list(tmp_path.glob(".watched-*"))
        assert len(tmp_files) == 0


class TestDefaultStatePath:
    def test_honors_wh_state_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        custom = tmp_path / "custom-watched.json"
        monkeypatch.setenv("WH_STATE_PATH", str(custom))
        assert _default_state_path() == custom

    def test_default_filename_is_watched_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("WH_STATE_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        path = _default_state_path()
        assert path.name == "watched.json"
        assert "worktrees-hives" in path.parts


class TestWatchlistAdd:
    """Tests for Watchlist.add."""

    def test_add_job(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "repo", "feature/x")
        assert job.job_id == "j1"
        assert job.owner == "acme"
        assert job.repo == "repo"
        assert job.branch == "feature/x"
        assert job.status == JobStatus.PENDING
        assert job.fix_count == 0
        assert job.max_fixes == 3

    def test_add_with_options(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "repo", "br", stack_id="s1", max_fixes=3)
        assert job.stack_id == "s1"
        assert job.max_fixes == 3

    def test_add_negative_max_fixes_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(ValueError, match="max_fixes"):
            watchlist.add("j1", "acme", "repo", "br", max_fixes=-1)

    def test_add_exceeds_safety_ceiling_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(PolicyError, match="safety ceiling"):
            watchlist.add("j1", "acme", "repo", "br", max_fixes=5)

    def test_add_duplicate_raises(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        with pytest.raises(ValueError, match="already exists"):
            watchlist.add("j1", "acme", "repo", "br")

    def test_add_persists(self, state_path: Path) -> None:
        w1 = Watchlist(state_path)
        w1.add("j1", "acme", "repo", "br")
        w2 = Watchlist(state_path)
        job = w2.get("j1")
        assert job is not None
        assert job.owner == "acme"

    def test_corrupt_json_raises_and_quarantines(self, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(CorruptStateError, match="quarantined"):
            Watchlist(state_path)
        # Original moved aside; no silent empty state
        assert not state_path.exists()
        quarantined = list(state_path.parent.glob("watched.json.corrupt.*"))
        assert len(quarantined) == 1


class TestMaxFixesOnLoad:
    def test_invalid_max_fixes_skipped(self, state_path: Path) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "bad": {
                            "job_id": "bad",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 99,
                            "fix_count": 0,
                            "residual_blockers": [],
                        },
                        "good": {
                            "job_id": "good",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 2,
                            "fix_count": 0,
                            "residual_blockers": [],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        w = Watchlist(state_path)
        assert w.get("bad") is None
        assert w.get("good") is not None
        assert w.get("good") is not None and w.get("good").max_fixes == 2


class TestWatchlistRemove:
    """Tests for Watchlist.remove."""

    def test_remove_job(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        watchlist.remove("j1")
        assert watchlist.get("j1") is None

    def test_remove_nonexistent_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(KeyError, match="not found"):
            watchlist.remove("nope")


class TestWatchlistGet:
    """Tests for Watchlist.get."""

    def test_get_existing(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.get("j1")
        assert job is not None
        assert job.job_id == "j1"

    def test_get_missing(self, watchlist: Watchlist) -> None:
        assert watchlist.get("nope") is None


class TestWatchlistList:
    """Tests for Watchlist.list_jobs."""

    def test_list_all(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        watchlist.add("j3", "example-org", "r3", "br")
        assert len(watchlist.list_jobs()) == 3

    def test_filter_by_owner(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "example-org", "r2", "br")
        jobs = watchlist.list_jobs(owner="acme")
        assert len(jobs) == 1
        assert jobs[0].owner == "acme"

    def test_filter_by_repo(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        jobs = watchlist.list_jobs(repo="r1")
        assert len(jobs) == 1

    def test_filter_by_status(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        jobs = watchlist.list_jobs(status=JobStatus.PENDING)
        assert len(jobs) == 1
        assert jobs[0].job_id == "j2"


class TestWatchlistUpdate:
    """Tests for Watchlist.update_status."""

    def test_update_status(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        assert job.status == JobStatus.IN_PROGRESS

    def test_update_nonexistent_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(KeyError, match="not found"):
            watchlist.update_status("nope", JobStatus.COMPLETED)


class TestWatchlistFixCount:
    """Tests for Watchlist.increment_fix_count."""

    def test_increment(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.increment_fix_count("j1")
        assert job.fix_count == 1
        assert job.fix_budget_remaining == 2

    def test_exhaust_budget_raises(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br", max_fixes=1)
        watchlist.increment_fix_count("j1")
        with pytest.raises(PolicyError, match="exhausted"):
            watchlist.increment_fix_count("j1")

    def test_safety_ceiling_enforced_on_increment(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if max_fixes were higher in memory, ceiling still caps increments."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        w = Watchlist(state_path)
        w.add("j1", "acme", "repo", "br", max_fixes=3)
        job = w.get("j1")
        assert job is not None
        # Simulate a corrupted in-memory max that still must hit the ceiling.
        job.max_fixes = 10
        for _ in range(3):
            w.increment_fix_count("j1")
        with pytest.raises(PolicyError, match="exhausted|ceiling"):
            w.increment_fix_count("j1")


class TestOwnerAllowlist:
    """Tests for WH_ALLOWED_OWNERS / allowed_owners on add."""

    def test_default_allowed_owners_empty(self) -> None:
        assert frozenset() == ALLOWED_OWNERS

    def test_empty_allowlist_allows_any(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        w = Watchlist(state_path)
        job = w.add("j1", "other-owner", "repo", "br")
        assert job.owner == "other-owner"

    def test_env_allowlist_rejects_other_owner(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        w = Watchlist(state_path)
        with pytest.raises(PolicyError, match="not in allowed owners"):
            w.add("j1", "other-owner", "repo", "br")
        w.add("j2", "acme", "repo", "br")
        assert w.get("j2") is not None

    def test_constructor_allowlist(self, state_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))
        with pytest.raises(PolicyError, match="not in allowed owners"):
            w.add("j1", "evil", "repo", "br")

    def test_load_allowed_owners_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme, example-org")
        assert load_allowed_owners_from_env() == frozenset({"acme", "example-org"})


class TestAdditiveV1Fields:
    """v1 schema: unknown job keys must not drop the job."""

    def test_additive_fields_preserved_on_roundtrip(self, state_path: Path) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "j1": {
                            "job_id": "j1",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 3,
                            "fix_count": 0,
                            "residual_blockers": [],
                            "kind": "issue",
                            "worktree_path": "/tmp/wt",
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        w = Watchlist(state_path)
        job = w.get("j1")
        assert job is not None
        assert job.owner == "acme"
        # Touch state so extras are written back
        w.update_status("j1", JobStatus.IN_PROGRESS)
        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        entry = reloaded["jobs"]["j1"]
        assert entry["kind"] == "issue"
        assert entry["worktree_path"] == "/tmp/wt"
        assert entry["created_at"] == "2026-01-01T00:00:00Z"
        assert entry["status"] == "in_progress"


class TestWatchlistBlockers:
    """Tests for Watchlist.set_blockers."""

    def test_set_blockers(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.set_blockers("j1", ["ci failing", "merge conflict"])
        assert job.residual_blockers == ["ci failing", "merge conflict"]

    def test_clear_blockers(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        watchlist.set_blockers("j1", ["blocker"])
        job = watchlist.set_blockers("j1", [])
        assert job.residual_blockers == []


class TestWatchlistPR:
    """Tests for Watchlist.set_pr."""

    def test_set_pr(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.set_pr("j1", 42, "https://github.com/acme/repo/pull/42")
        assert job.pr_number == 42
        assert job.pr_url == "https://github.com/acme/repo/pull/42"


class TestWatchlistCheck:
    """Tests for Watchlist.check."""

    def test_empty(self, watchlist: Watchlist) -> None:
        result = watchlist.check()
        assert all(len(v) == 0 for v in result.values())

    def test_categorize(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        watchlist.add("j3", "acme", "r3", "br")
        watchlist.add("j4", "acme", "r4", "br")
        watchlist.set_pr("j2", 1, "https://example.com/pr/1")
        # j2 is green with PR and budget — ready, NOT needs_fix
        watchlist.set_blockers("j3", ["failing test"])
        watchlist.update_status("j4", JobStatus.COMPLETED)

        result = watchlist.check()
        assert len(result["needs_pr"]) == 1
        assert result["needs_pr"][0].job_id == "j1"
        assert len(result["ready"]) == 1
        assert result["ready"][0].job_id == "j2"
        assert len(result["needs_fix"]) == 1
        assert result["needs_fix"][0].job_id == "j3"
        assert len(result["done"]) == 1
        assert result["done"][0].job_id == "j4"

    def test_exhausted_budget_with_blockers_is_blocked(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br", max_fixes=1)
        watchlist.set_pr("j1", 1, "https://example.com/pr/1")
        watchlist.increment_fix_count("j1")
        watchlist.set_blockers("j1", ["still failing"])
        result = watchlist.check()
        assert len(result["blocked"]) == 1
        assert result["blocked"][0].job_id == "j1"
        assert result["ready"] == []
        assert result["needs_fix"] == []

    def test_green_pr_with_budget_is_ready(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.set_pr("j1", 9, "https://example.com/pr/9")
        result = watchlist.check()
        assert result["needs_fix"] == []
        assert len(result["ready"]) == 1


class TestJobState:
    """Tests for JobState properties."""

    def test_full_repo(self) -> None:
        job = JobState("j1", "acme", "repo", "br")
        assert job.full_repo == "acme/repo"

    def test_is_actionable(self) -> None:
        job = JobState("j1", "acme", "repo", "br")
        assert job.is_actionable is True
        job.status = JobStatus.COMPLETED
        assert job.is_actionable is False

    def test_fix_budget(self) -> None:
        job = JobState("j1", "acme", "repo", "br", fix_count=2, max_fixes=3)
        assert job.fix_budget_remaining == 1
        job.fix_count = 3
        assert job.fix_budget_remaining == 0


class TestMultiOwner:
    """Tests for multi-owner repo support (generic owners)."""

    def test_acme_owner(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "widgets", "feature/x")
        assert job.full_repo == "acme/widgets"

    def test_example_org_owner(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j2", "example-org", "project", "main")
        assert job.full_repo == "example-org/project"

    def test_filter_across_owners(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "example-org", "r2", "br")
        watchlist.add("j3", "acme", "r3", "br")
        acme_jobs = watchlist.list_jobs(owner="acme")
        example_jobs = watchlist.list_jobs(owner="example-org")
        assert len(acme_jobs) == 2
        assert len(example_jobs) == 1


class TestCliPolicyExit:
    """CLI maps PolicyError to exit code 2."""

    def test_add_max_fixes_policy_returns_2(self, tmp_path: Path) -> None:
        from worktrees_hives.cli import main

        code = main(
            [
                "--state",
                str(tmp_path / "watched.json"),
                "watchlist",
                "add",
                "j1",
                "acme",
                "repo",
                "br",
                "--max-fixes",
                "4",
            ]
        )
        assert code == 2

    def test_add_duplicate_returns_1(self, tmp_path: Path) -> None:
        from worktrees_hives.cli import main

        state = str(tmp_path / "watched.json")
        assert main(["--state", state, "watchlist", "add", "j1", "acme", "repo", "br"]) == 0
        assert main(["--state", state, "watchlist", "add", "j1", "acme", "repo", "br"]) == 1

    def test_prog_is_worktrees_hives(self) -> None:
        from worktrees_hives.cli import main

        with pytest.raises(SystemExit):
            main(["--help"])
