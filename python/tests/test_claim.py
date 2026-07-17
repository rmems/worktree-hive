"""Tests for worktrees_hives.claim \u2014 issue/PR claim and worktree isolation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.bridge import WhClient
from worktrees_hives.claim import (
    ClaimError,
    ClaimExistsError,
    ClaimManager,
    ClaimResult,
    IsolationError,
    _validate_ref,
    _validate_segment,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_base(tmp_path: Path) -> str:
    """Provide a temporary worktree base directory."""
    return str(tmp_path / "worktrees")


@pytest.fixture
def mock_wh() -> WhClient:
    """Provide a WhClient with a mocked binary path."""
    return WhClient(wh_path="/usr/bin/true", timeout=5.0)


@pytest.fixture
def manager(mock_wh: WhClient, tmp_base: str, monkeypatch: pytest.MonkeyPatch) -> ClaimManager:
    """Provide a ClaimManager with a temp base and mock wh client.

    Identity binding is stubbed out so claim-flow tests can use generic
    owners without a matching origin. See TestClaimIdentity for identity tests.
    """
    mgr = ClaimManager(
        wh_client=mock_wh,
        worktree_base=tmp_base,
        allowed_owners=frozenset(),
    )
    monkeypatch.setattr(mgr, "_assert_claim_identity", lambda owner, repo: None)
    return mgr


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidateSegment:
    """Tests for the _validate_segment helper."""

    def test_accepts_valid_segments(self) -> None:
        _validate_segment("owner", "acme")
        _validate_segment("repo", "example-repo")
        _validate_segment("job_id", "gh-42")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ClaimError, match="Invalid owner"):
            _validate_segment("owner", "")

    def test_rejects_dot(self) -> None:
        with pytest.raises(ClaimError, match="Invalid repo"):
            _validate_segment("repo", ".")

    def test_rejects_double_dot(self) -> None:
        with pytest.raises(ClaimError, match="Invalid job_id"):
            _validate_segment("job_id", "..")

    def test_rejects_slash(self) -> None:
        with pytest.raises(ClaimError, match="separator"):
            _validate_segment("owner", "acme/repo")

    def test_rejects_backslash(self) -> None:
        with pytest.raises(ClaimError, match="separator"):
            _validate_segment("repo", "foo\\bar")

    def test_rejects_colon(self) -> None:
        with pytest.raises(ClaimError, match="separator"):
            _validate_segment("job_id", "C:drive")


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------


class TestDerivePath:
    """Tests for worktree path derivation."""

    def test_standard_path(self, manager: ClaimManager) -> None:
        path = manager._derive_path("acme", "example-repo", "gh-42")
        expected = os.path.join(manager.worktree_base, "acme", "example-repo", "gh-42")
        assert path == expected

    def test_path_includes_all_segments(self, manager: ClaimManager) -> None:
        path = manager._derive_path("owner", "repo", "job-123")
        assert path.endswith(os.path.join("owner", "repo", "job-123"))

    def test_rejects_traversal_in_owner(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError):
            manager._derive_path("../evil", "repo", "job")

    def test_rejects_traversal_in_repo(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError):
            manager._derive_path("owner", "..", "job")

    def test_rejects_traversal_in_job_id(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError):
            manager._derive_path("owner", "repo", "../../etc")


# ---------------------------------------------------------------------------
# claim_issue
# ---------------------------------------------------------------------------


class TestClaimIssue:
    """Tests for claiming a GitHub issue."""

    @patch("worktrees_hives.claim.subprocess.run")
    def test_creates_branch_and_worktree(
        self, mock_run: MagicMock, manager: ClaimManager, tmp_path: Path
    ) -> None:
        # All git commands succeed
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Override _verify_isolation to return True
        with patch.object(manager, "_verify_isolation", return_value=True):
            result = manager.claim_issue("acme", "example-repo", 42)

        assert result.owner == "acme"
        assert result.repo == "example-repo"
        assert result.issue_number == 42
        assert result.branch == "hive/gh-42"
        assert result.job_id == "gh-42"
        assert "gh-42" in result.worktree_path

    def test_rejects_existing_worktree(self, manager: ClaimManager, tmp_path: Path) -> None:
        # Pre-create the worktree directory
        worktree_path = manager._derive_path("acme", "example-repo", "gh-1")
        os.makedirs(worktree_path, exist_ok=True)

        with pytest.raises(ClaimExistsError, match="already exists"):
            manager.claim_issue("acme", "example-repo", 1)

    @patch("worktrees_hives.claim.subprocess.run")
    def test_cleans_up_branch_on_worktree_failure(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        call_count = 0

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # First call: branch creation succeeds
            if "branch" in cmd and "-D" not in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            # Second call: worktree add fails
            if "worktree" in cmd and "add" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="disk full")
            # Cleanup calls
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with pytest.raises(ClaimError, match=r"git worktree add .* failed \(exit 1\): disk full"):
            manager.claim_issue("acme", "example-repo", 99)

    def test_rejects_invalid_owner(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError, match="Invalid owner"):
            manager.claim_issue("", "repo", 1)

    def test_rejects_invalid_repo(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError, match="Invalid repo"):
            manager.claim_issue("owner", "../evil", 1)

    @patch("worktrees_hives.claim.subprocess.run")
    def test_custom_base_ref(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(manager, "_verify_isolation", return_value=True):
            manager.claim_issue("acme", "repo", 7, base_ref="origin/develop")

        # Verify the branch was created from origin/develop
        calls = mock_run.call_args_list
        branch_call = next(
            c
            for c in calls
            if "branch" in c[0][0] and "-D" not in c[0][0] and "push" not in c[0][0]
        )
        assert "origin/develop" in branch_call[0][0]

    @patch("worktrees_hives.claim.subprocess.run")
    def test_publishes_upstream_for_issue_branch(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(manager, "_verify_isolation", return_value=True):
            manager.claim_issue("acme", "example-repo", 42)

        calls = [c[0][0] for c in mock_run.call_args_list]
        push_calls = [c for c in calls if "push" in c and "-u" in c]
        assert push_calls, "expected git push -u origin for issue branch"
        assert "origin" in push_calls[0]
        assert "hive/gh-42" in push_calls[0]


# ---------------------------------------------------------------------------
# claim_pr
# ---------------------------------------------------------------------------


PR_HEAD_SHA = "a" * 40


def _default_pr_run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
    """Default subprocess side effect for successful PR claim paths."""
    if "fetch" in cmd:
        return MagicMock(returncode=0, stdout="", stderr="")
    if "worktree" in cmd and "list" in cmd:
        return MagicMock(returncode=0, stdout="", stderr="")
    if "rev-parse" in cmd:
        if "HEAD" in cmd:
            return MagicMock(returncode=0, stdout="local-sha\n", stderr="")
        return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
    return MagicMock(returncode=0, stdout="ok\n", stderr="")


class TestClaimPR:
    """Tests for claiming a GitHub PR."""

    @patch("worktrees_hives.claim.subprocess.run")
    def test_uses_existing_branch(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.side_effect = _default_pr_run_side_effect

        with patch.object(manager, "_verify_isolation", return_value=True):
            result = manager.claim_pr(
                "acme",
                "example-repo",
                15,
                head_branch="feature/existing-pr",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

        assert result.pr_number == 15
        assert result.branch == "feature/existing-pr"
        assert result.job_id == "pr-15"

    @patch("worktrees_hives.claim.subprocess.run")
    def test_uses_custom_head_branch(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.side_effect = _default_pr_run_side_effect

        with patch.object(manager, "_verify_isolation", return_value=True):
            result = manager.claim_pr(
                "acme",
                "repo",
                10,
                head_branch="feature/my-pr",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

        assert result.branch == "feature/my-pr"
        assert result.job_id == "pr-10"

    def test_rejects_existing_worktree(self, manager: ClaimManager, tmp_path: Path) -> None:
        worktree_path = manager._derive_path("acme", "repo", "pr-5")
        os.makedirs(worktree_path, exist_ok=True)

        with pytest.raises(ClaimExistsError, match="already exists"):
            manager.claim_pr(
                "acme",
                "repo",
                5,
                head_branch="feature/pr-head",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

    def test_requires_head_branch(self, manager: ClaimManager) -> None:
        with pytest.raises(TypeError):
            manager.claim_pr("acme", "repo", 5)  # type: ignore[call-arg]

    def test_accepts_plus_in_branch_name(self) -> None:
        _validate_ref("branch", "feature/foo+bar")

    @patch("worktrees_hives.claim.subprocess.run")
    def test_surfaces_stale_tip_sync_failure(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """When worktree HEAD is behind remote and reset fails, raise ClaimError."""

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "worktree" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "reset" in cmd and "--hard" in cmd:
                return MagicMock(
                    returncode=1,
                    stdout="",
                    stderr="fatal: cannot reset branch checked out elsewhere",
                )
            if "merge-base" in cmd and "--is-ancestor" in cmd:
                i = cmd.index("--is-ancestor")
                first, second = cmd[i + 1], cmd[i + 2]
                # HEAD is ancestor of remote → local behind remote (safe reset).
                if first == "HEAD" and second != "HEAD":
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=1, stdout="", stderr="")
            if "rev-parse" in cmd:
                if "HEAD" in cmd:
                    return MagicMock(returncode=0, stdout="local-sha\n", stderr="")
                return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
            return MagicMock(returncode=0, stdout="ok\n", stderr="")

        mock_run.side_effect = side_effect

        with (
            patch.object(manager, "_verify_isolation", return_value=True),
            pytest.raises(ClaimError, match=r"reset|--hard|failed"),
        ):
            manager.claim_pr(
                "acme",
                "example-repo",
                9,
                head_branch="feature/pr-head",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

    @patch("worktrees_hives.claim.subprocess.run")
    def test_preserves_ahead_tip_without_hard_reset(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """Local tip ahead of remote must not be hard-reset (keeps unpushed commits)."""
        reset_calls: list[list[str]] = []

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "worktree" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "reset" in cmd and "--hard" in cmd:
                reset_calls.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "merge-base" in cmd and "--is-ancestor" in cmd:
                i = cmd.index("--is-ancestor")
                first, second = cmd[i + 1], cmd[i + 2]
                if first == "HEAD" and second != "HEAD":
                    return MagicMock(returncode=1, stdout="", stderr="")  # not behind
                if first != "HEAD" and second == "HEAD":
                    return MagicMock(returncode=0, stdout="", stderr="")  # ahead
                return MagicMock(returncode=1, stdout="", stderr="")
            if "rev-parse" in cmd:
                if "HEAD" in cmd:
                    return MagicMock(returncode=0, stdout="local-ahead-sha\n", stderr="")
                return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
            return MagicMock(returncode=0, stdout="ok\n", stderr="")

        mock_run.side_effect = side_effect

        with patch.object(manager, "_verify_isolation", return_value=True):
            result = manager.claim_pr(
                "acme",
                "example-repo",
                11,
                head_branch="feature/pr-head",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

        assert result.branch == "feature/pr-head"
        assert reset_calls == [], "must not hard-reset when local tip is ahead of origin"

    @patch("worktrees_hives.claim.subprocess.run")
    def test_diverged_tip_raises_without_reset(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """Diverged local/remote tips must raise ClaimError, not discard commits."""
        reset_calls: list[list[str]] = []

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "worktree" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "reset" in cmd and "--hard" in cmd:
                reset_calls.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "merge-base" in cmd and "--is-ancestor" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="")  # neither ancestor
            if "rev-parse" in cmd:
                if "HEAD" in cmd:
                    return MagicMock(returncode=0, stdout="local-sha-xyz\n", stderr="")
                return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
            return MagicMock(returncode=0, stdout="ok\n", stderr="")

        mock_run.side_effect = side_effect

        with (
            patch.object(manager, "_verify_isolation", return_value=True),
            pytest.raises(ClaimError, match="diverged"),
        ):
            manager.claim_pr(
                "acme",
                "example-repo",
                12,
                head_branch="feature/pr-head",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

        assert reset_calls == [], "must not hard-reset when histories have diverged"

    @patch("worktrees_hives.claim.subprocess.run")
    def test_rejects_branch_with_mismatched_sha(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """Reject claim when existing head_branch points to different SHA than PR_HEAD_SHA."""
        stale_sha = "b" * 40

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            joined = " ".join(cmd)
            if "check-ref-format" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "worktree" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "update-ref" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-parse" in cmd:
                if "refs/worktrees-hives" in joined:
                    return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
                if "refs/remotes/origin/" in joined:
                    return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
                if "--verify" in cmd and "feature/stale-branch" in joined:
                    return MagicMock(returncode=0, stdout=f"{stale_sha}\n", stderr="")
                return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
            return MagicMock(returncode=0, stdout="ok\n", stderr="")

        mock_run.side_effect = side_effect

        with pytest.raises(ClaimError, match=r"Local branch .* points to|Refusing to claim"):
            manager.claim_pr(
                "acme",
                "example-repo",
                20,
                head_branch="feature/stale-branch",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

    @patch("worktrees_hives.claim.subprocess.run")
    def test_pin_sha_blocks_reset_when_upstream_differs(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """_sync_worktree_to_remote must not hard-reset when pin_sha != remote_sha."""
        other = "f" * 40
        reset_calls: list[list[str]] = []

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "reset" in cmd and "--hard" in cmd:
                reset_calls.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "merge-base" in cmd and "--is-ancestor" in cmd:
                # HEAD behind remote → would normally reset
                i = cmd.index("--is-ancestor")
                if cmd[i + 1] == "HEAD":
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=1, stdout="", stderr="")
            if "rev-parse" in cmd:
                joined = " ".join(cmd)
                if "origin/" in joined or "refs/remotes/" in joined:
                    return MagicMock(returncode=0, stdout=f"{other}\n", stderr="")
                if "HEAD" in cmd:
                    return MagicMock(returncode=0, stdout=f"{PR_HEAD_SHA}\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        manager._sync_worktree_to_remote(
            "/tmp/wt",
            "feature/x",
            upstream_ref="origin/feature/x",
            pin_sha=PR_HEAD_SHA,
        )
        assert reset_calls == []


# ---------------------------------------------------------------------------
# verify_isolation
# ---------------------------------------------------------------------------


class TestVerifyIsolation:
    """Tests for branch verification before edits."""

    @patch("worktrees_hives.claim.subprocess.run")
    def test_passes_on_match(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="hive/gh-42\n", stderr="")

        assert manager.verify_isolation("/some/path", "hive/gh-42") is True

    @patch("worktrees_hives.claim.subprocess.run")
    def test_fails_on_mismatch(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")

        with pytest.raises(IsolationError, match="Branch mismatch"):
            manager.verify_isolation("/some/path", "hive/gh-42")

    @patch("worktrees_hives.claim.subprocess.run")
    def test_fails_on_git_error(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="fatal")

        with pytest.raises(IsolationError, match="Cannot determine HEAD"):
            manager.verify_isolation("/some/path", "hive/gh-42")


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for worktree cleanup."""

    @patch("worktrees_hives.claim.subprocess.run")
    def test_removes_worktree_and_branch(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        wt = os.path.join(manager.worktree_base, "acme", "example-repo", "gh-42")
        manager.cleanup(wt, "hive/gh-42", delete_branch=True, owns_branch=True)

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in c and "remove" in c for c in calls)
        assert any("branch" in c and "-D" in c for c in calls)
        assert any("worktree" in c and "prune" in c for c in calls)

    @patch("worktrees_hives.claim.subprocess.run")
    def test_does_not_delete_branch_by_default(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        wt = os.path.join(manager.worktree_base, "acme", "example-repo", "pr-1")
        manager.cleanup(wt, "feature/pr-head")

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in c and "remove" in c for c in calls)
        assert not any("branch" in c and "-D" in c for c in calls)

    @patch("worktrees_hives.claim.subprocess.run")
    def test_skips_prune_when_disabled(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        wt = os.path.join(manager.worktree_base, "acme", "example-repo", "gh-42")
        manager.cleanup(wt, "hive/gh-42", prune=False)

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert not any("prune" in c for c in calls)

    @patch("worktrees_hives.claim.subprocess.run")
    def test_ignores_remove_failure(self, mock_run: MagicMock, manager: ClaimManager) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")

        # Should not raise — cleanup is best-effort
        wt = os.path.join(manager.worktree_base, "acme", "example-repo", "missing")
        manager.cleanup(wt, "hive/gh-42")


# ---------------------------------------------------------------------------
# ClaimResult
# ---------------------------------------------------------------------------


class TestClaimResult:
    """Tests for the ClaimResult dataclass."""

    def test_fields(self) -> None:
        result = ClaimResult(
            owner="acme",
            repo="example-repo",
            job_id="gh-42",
            branch="hive/gh-42",
            worktree_path="/tmp/acme/example-repo/gh-42",
            issue_number=42,
        )
        assert result.owner == "acme"
        assert result.issue_number == 42
        assert result.pr_number is None

    def test_pr_result(self) -> None:
        result = ClaimResult(
            owner="acme",
            repo="repo",
            job_id="pr-10",
            branch="feature/foo",
            worktree_path="/tmp/pr-10",
            pr_number=10,
        )
        assert result.pr_number == 10
        assert result.issue_number is None


# ---------------------------------------------------------------------------
# Sandbox + option-injection rejection
# ---------------------------------------------------------------------------


class TestSandboxAndOptionInjection:
    """Safety: path sandbox and option-looking ref rejection."""

    def test_relative_worktree_base_resolved_absolute(
        self, mock_wh: WhClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Relative WH_WORKTREE_BASE must not be interpreted under repo_root."""
        monkeypatch.chdir(tmp_path)
        rel = "rel-worktrees"
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=rel,
            repo_root=str(tmp_path / "other-repo"),
        )
        assert os.path.isabs(mgr.worktree_base)
        assert mgr.worktree_base == os.path.abspath(rel)
        path = mgr._derive_path("acme", "example-repo", "gh-1")
        assert os.path.isabs(path)
        assert path.startswith(mgr.worktree_base)

    def test_rejects_option_looking_base_ref(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError, match=r"base_ref|option-looking|Invalid"):
            manager.claim_issue("acme", "example-repo", 1, base_ref="--force")

    def test_rejects_option_looking_head_branch(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError, match=r"branch|option-looking|Invalid"):
            manager.claim_pr(
                "acme",
                "example-repo",
                1,
                head_branch="-f",
                head_repo="origin",
                head_sha=PR_HEAD_SHA,
            )

    def test_validate_ref_rejects_force(self) -> None:
        with pytest.raises(ClaimError, match=r"option-looking|Invalid"):
            _validate_ref("base_ref", "--force")

    def test_remove_outside_sandbox_raises(self, manager: ClaimManager) -> None:
        with patch("worktrees_hives.claim.subprocess.run") as mock_run:
            with pytest.raises(ClaimError, match="escapes sandbox"):
                manager._remove_worktree("/tmp/evil-escape")
            mock_run.assert_not_called()

    def test_cleanup_aborts_after_sandbox_rejection(self, manager: ClaimManager) -> None:
        """Sandbox reject must not delete branches or prune."""
        with patch("worktrees_hives.claim.subprocess.run") as mock_run:
            with pytest.raises(ClaimError, match="escapes sandbox"):
                manager.cleanup(
                    "/tmp/evil-escape",
                    "hive/gh-1",
                    delete_branch=True,
                    prune=True,
                )
            mock_run.assert_not_called()

    def test_assert_under_base_rejects_escape(self, manager: ClaimManager) -> None:
        with pytest.raises(ClaimError, match="escapes sandbox"):
            manager._assert_under_base("/etc/passwd")


# ---------------------------------------------------------------------------
# Branch materialization (single-branch clones)
# ---------------------------------------------------------------------------


class TestBranchMaterialization:
    """Tests for single-branch clone branch materialization."""

    @patch("worktrees_hives.claim.subprocess.run")
    def test_materialize_local_branch_uses_sha_not_track(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """Materialize from SHA; never use git branch --track origin/<name>."""
        sha = "a" * 40

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:3] == ["git", "branch", "-u"]:
                return MagicMock(
                    returncode=128,
                    stdout="",
                    stderr="starting point is not a branch",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        manager._materialize_local_branch("feature/foo", sha, "origin/feature/foo")

        calls = [c[0][0] for c in mock_run.call_args_list]
        branch_calls = [c for c in calls if c[:2] == ["git", "branch"]]
        assert any(c == ["git", "branch", "--", "feature/foo", sha] for c in branch_calls)
        assert not any("--track" in c for c in branch_calls)
        config_calls = [c for c in calls if c[:2] == ["git", "config"]]
        assert any("branch.feature/foo.remote" in c for c in config_calls)
        assert any("refs/heads/feature/foo" in c for c in config_calls)

    @patch("worktrees_hives.claim.subprocess.run")
    def test_ensure_branch_exists_from_remote_tracking(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """Create missing local branch from refs/remotes/origin/<name>."""
        sha = "c" * 40

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:4] == ["git", "rev-parse", "--verify", "feature/pr-head"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if cmd[:4] == ["git", "rev-parse", "--verify", "refs/remotes/origin/feature/pr-head"]:
                return MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            if cmd[:3] == ["git", "branch", "--"] and "feature/pr-head" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        manager._ensure_branch_exists("feature/pr-head")

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any(c[:5] == ["git", "branch", "--", "feature/pr-head", sha] for c in calls), (
            "expected branch creation at remote-tracking SHA"
        )

    @patch("worktrees_hives.claim.subprocess.run")
    def test_ensure_origin_tracking_ref_creates_remote_ref(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        sha = "d" * 40
        fetch_calls: list[list[str]] = []

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:3] == ["git", "fetch", "origin"]:
                fetch_calls.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:4] == ["git", "rev-parse", "--verify", "refs/remotes/origin/feature/x"]:
                return MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        upstream = manager._ensure_origin_tracking_ref("feature/x", sha)

        assert upstream == "origin/feature/x"
        assert any(
            "refs/heads/feature/x:refs/remotes/origin/feature/x" in " ".join(c) for c in fetch_calls
        )


class TestClaimIdentity:
    """Tests for origin identity binding and optional owner allowlist."""

    def test_default_empty_allowlist(self, mock_wh: WhClient, tmp_base: str) -> None:
        """Unset WH_ALLOWED_OWNERS yields empty allowlist (identity still enforced)."""
        mgr = ClaimManager(wh_client=mock_wh, worktree_base=tmp_base)
        assert mgr.allowed_owners == frozenset()

    def test_wildcard_env_allows_all_owners(
        self, mock_wh: WhClient, tmp_base: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "*")
        mgr = ClaimManager(wh_client=mock_wh, worktree_base=tmp_base)
        assert mgr.allowed_owners == frozenset()

    def test_explicit_allowlist_from_env(
        self, mock_wh: WhClient, tmp_base: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme,foobar,example")
        mgr = ClaimManager(wh_client=mock_wh, worktree_base=tmp_base)
        assert mgr.allowed_owners == frozenset(["acme", "foobar", "example"])

    def test_rejects_owner_not_in_allowlist_when_origin_matches(
        self, mock_wh: WhClient, tmp_base: str
    ) -> None:
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=tmp_base,
            allowed_owners=frozenset(["other-org"]),
        )
        with (
            patch("worktrees_hives.claim.subprocess.run") as mock_run,
            pytest.raises(ClaimError, match=r"not in the configured allowlist"),
        ):

            def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
                if "config" in cmd and "remote.origin.url" in cmd:
                    return MagicMock(
                        returncode=0,
                        stdout="https://github.com/acme/example-repo.git\n",
                        stderr="",
                    )
                return MagicMock(returncode=1, stdout="", stderr="")

            mock_run.side_effect = side_effect
            mgr._assert_claim_identity("acme", "example-repo")

    def test_identity_mismatch_even_with_empty_allowlist(
        self, mock_wh: WhClient, tmp_base: str
    ) -> None:
        """Empty allowlist still requires origin owner/repo match."""
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=tmp_base,
            allowed_owners=frozenset(),
        )
        with (
            patch("worktrees_hives.claim.subprocess.run") as mock_run,
            pytest.raises(ClaimError, match=r"Identity mismatch"),
        ):

            def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
                if "config" in cmd and "remote.origin.url" in cmd:
                    return MagicMock(
                        returncode=0,
                        stdout="https://github.com/acme/real-repo.git\n",
                        stderr="",
                    )
                return MagicMock(returncode=1, stdout="", stderr="")

            mock_run.side_effect = side_effect
            mgr._assert_claim_identity("acme", "fake-repo")

    def test_unparseable_origin_fails_closed(self, mock_wh: WhClient, tmp_base: str) -> None:
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=tmp_base,
            allowed_owners=frozenset(),
        )
        with (
            patch("worktrees_hives.claim.subprocess.run") as mock_run,
            pytest.raises(ClaimError, match=r"Cannot verify repository identity"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            mgr._assert_claim_identity("acme", "example-repo")

    def test_unparseable_origin_override(
        self, mock_wh: WhClient, tmp_base: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WH_ALLOW_UNVERIFIED_REMOTE", "1")
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=tmp_base,
            allowed_owners=frozenset(),
        )
        with patch("worktrees_hives.claim.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            mgr._assert_claim_identity("any-owner", "any-repo")

    def test_canonical_identity_match_accepted(self, mock_wh: WhClient, tmp_base: str) -> None:
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=tmp_base,
            allowed_owners=frozenset(["acme"]),
        )
        with patch("worktrees_hives.claim.subprocess.run") as mock_run:

            def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
                if "config" in cmd and "remote.origin.url" in cmd:
                    return MagicMock(
                        returncode=0,
                        stdout="https://github.com/acme/example-repo.git\n",
                        stderr="",
                    )
                return MagicMock(returncode=1, stdout="", stderr="")

            mock_run.side_effect = side_effect
            mgr._assert_claim_identity("acme", "example-repo")

    def test_repo_name_ending_in_git_chars_not_stripped(
        self, mock_wh: WhClient, tmp_base: str
    ) -> None:
        mgr = ClaimManager(
            wh_client=mock_wh,
            worktree_base=tmp_base,
            allowed_owners=frozenset(["someowner"]),
        )
        with patch("worktrees_hives.claim.subprocess.run") as mock_run:

            def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
                if "config" in cmd and "remote.origin.url" in cmd:
                    return MagicMock(
                        returncode=0,
                        stdout="https://github.com/someowner/toolkit.git\n",
                        stderr="",
                    )
                return MagicMock(returncode=1, stdout="", stderr="")

            mock_run.side_effect = side_effect
            mgr._assert_claim_identity("someowner", "toolkit")

    def test_validate_head_repo_rejects_path(self) -> None:
        from worktrees_hives.claim import _validate_head_repo

        with pytest.raises(ClaimError, match="head_repo"):
            _validate_head_repo("/tmp/evil")

    @patch("worktrees_hives.claim.subprocess.run")
    def test_ensure_origin_tracking_pins_sha(
        self, mock_run: MagicMock, manager: ClaimManager
    ) -> None:
        """Tracking ref is always pinned to verified SHA, never FETCH_HEAD."""
        sha = "a" * 40
        update_refs: list[list[str]] = []

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "update-ref" in cmd:
                update_refs.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-parse" in cmd and "refs/remotes/origin/" in " ".join(cmd):
                return MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            if "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "check-ref-format" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")

        mock_run.side_effect = side_effect
        upstream = manager._ensure_origin_tracking_ref("feature/x", sha)
        assert upstream == "origin/feature/x"
        assert any(sha in " ".join(c) for c in update_refs)
