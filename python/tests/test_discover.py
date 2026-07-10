"""Tests for the discovery module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.discover import (
    ALLOWED_OWNERS,
    DiscoveryResult,
    Issue,
    IssueState,
    _parse_issue,
    discover_all,
    discover_issues_for_repo,
    discover_pull_requests_for_repo,
    filter_issues,
    format_for_orchestrator,
    list_repos_for_owner,
)


@pytest.fixture
def sample_issue_data() -> dict:
    """Sample GitHub issue JSON data."""
    return {
        "number": 42,
        "title": "Test Issue",
        "state": "OPEN",
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "milestone": {"title": "v1.0"},
        "html_url": "https://github.com/rmems/test-repo/issues/42",
        "assignees": [{"login": "user1"}, {"login": "user2"}],
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-02T00:00:00Z",
    }


@pytest.fixture
def sample_pr_data() -> dict:
    """Sample GitHub PR JSON data."""
    return {
        "number": 10,
        "title": "Test PR",
        "state": "OPEN",
        "labels": [{"name": "enhancement"}],
        "milestone": None,
        "html_url": "https://github.com/rmems/test-repo/pull/10",
        "assignees": [{"login": "user1"}],
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-03T00:00:00Z",
        "pullRequest": {"url": "https://api.github.com/repos/rmems/test-repo/pulls/10"},
    }


@pytest.fixture
def sample_issue(sample_issue_data: dict) -> Issue:
    """Sample Issue object."""
    return _parse_issue(sample_issue_data, "rmems", "test-repo")


@pytest.fixture
def sample_pr(sample_pr_data: dict) -> Issue:
    """Sample Issue object representing a PR."""
    issue = _parse_issue(sample_pr_data, "rmems", "test-repo")
    return Issue(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        labels=issue.labels,
        milestone=issue.milestone,
        url=issue.url,
        owner=issue.owner,
        repo=issue.repo,
        is_pr=True,
        assignees=issue.assignees,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


class TestParseIssue:
    """Tests for _parse_issue function."""

    def test_parse_issue_basic(self, sample_issue_data: dict) -> None:
        """Test parsing a basic issue."""
        issue = _parse_issue(sample_issue_data, "rmems", "test-repo")

        assert issue.number == 42
        assert issue.title == "Test Issue"
        assert issue.state == "open"
        assert issue.labels == ["bug", "priority:high"]
        assert issue.milestone == "v1.0"
        assert issue.url == "https://github.com/rmems/test-repo/issues/42"
        assert issue.owner == "rmems"
        assert issue.repo == "test-repo"
        assert issue.is_pr is False
        assert issue.assignees == ["user1", "user2"]
        assert issue.created_at == "2025-01-01T00:00:00Z"
        assert issue.updated_at == "2025-01-02T00:00:00Z"

    def test_parse_issue_no_milestone(self, sample_issue_data: dict) -> None:
        """Test parsing an issue without a milestone."""
        sample_issue_data["milestone"] = None
        issue = _parse_issue(sample_issue_data, "rmems", "test-repo")

        assert issue.milestone is None

    def test_parse_issue_no_labels(self, sample_issue_data: dict) -> None:
        """Test parsing an issue without labels."""
        sample_issue_data["labels"] = []
        issue = _parse_issue(sample_issue_data, "rmems", "test-repo")

        assert issue.labels == []

    def test_parse_issue_no_assignees(self, sample_issue_data: dict) -> None:
        """Test parsing an issue without assignees."""
        sample_issue_data["assignees"] = []
        issue = _parse_issue(sample_issue_data, "rmems", "test-repo")

        assert issue.assignees == []

    def test_parse_issue_with_pull_request(self, sample_pr_data: dict) -> None:
        """Test parsing a pull request."""
        issue = _parse_issue(sample_pr_data, "rmems", "test-repo")

        assert issue.is_pr is True


class TestDiscoverIssuesForRepo:
    """Tests for discover_issues_for_repo function."""

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_success(self, mock_run_gh: MagicMock, sample_issue_data: dict) -> None:
        """Test successful issue discovery."""
        mock_run_gh.return_value = (json.dumps([sample_issue_data]), "", 0)

        issues, error = discover_issues_for_repo("rmems", "test-repo")

        assert error is None
        assert len(issues) == 1
        assert issues[0].number == 42
        mock_run_gh.assert_called_once_with([
            "issue", "list",
            "--repo", "rmems/test-repo",
            "--state", "open",
            "--json", "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
            "--limit", "100",
        ])

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_repo_not_found(self, mock_run_gh: MagicMock) -> None:
        """Test handling of non-existent repository."""
        mock_run_gh.return_value = ("", "Could not resolve to a Repository", 1)

        issues, error = discover_issues_for_repo("rmems", "nonexistent-repo")

        assert issues == []
        assert error is None

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_other_error(self, mock_run_gh: MagicMock) -> None:
        """Test handling of other errors."""
        mock_run_gh.return_value = ("", "Authentication failed", 1)

        issues, error = discover_issues_for_repo("rmems", "test-repo")

        assert issues == []
        assert error is not None
        assert "Authentication failed" in error

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_json_parse_error(self, mock_run_gh: MagicMock) -> None:
        """Test handling of JSON parse errors."""
        mock_run_gh.return_value = ("invalid json", "", 0)

        issues, error = discover_issues_for_repo("rmems", "test-repo")

        assert issues == []
        assert error is not None
        assert "Failed to parse JSON" in error

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_with_state_filter(self, mock_run_gh: MagicMock, sample_issue_data: dict) -> None:
        """Test issue discovery with state filter."""
        mock_run_gh.return_value = (json.dumps([sample_issue_data]), "", 0)

        issues, error = discover_issues_for_repo("rmems", "test-repo", IssueState.CLOSED)

        assert error is None
        mock_run_gh.assert_called_once_with([
            "issue", "list",
            "--repo", "rmems/test-repo",
            "--state", "closed",
            "--json", "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
            "--limit", "100",
        ])


class TestDiscoverPullRequestsForRepo:
    """Tests for discover_pull_requests_for_repo function."""

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_prs_success(self, mock_run_gh: MagicMock, sample_pr_data: dict) -> None:
        """Test successful PR discovery."""
        mock_run_gh.return_value = (json.dumps([sample_pr_data]), "", 0)

        prs, error = discover_pull_requests_for_repo("rmems", "test-repo")

        assert error is None
        assert len(prs) == 1
        assert prs[0].number == 10
        assert prs[0].is_pr is True

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_prs_repo_not_found(self, mock_run_gh: MagicMock) -> None:
        """Test handling of non-existent repository for PRs."""
        mock_run_gh.return_value = ("", "Could not resolve to a Repository", 1)

        prs, error = discover_pull_requests_for_repo("rmems", "nonexistent-repo")

        assert prs == []
        assert error is None


class TestListReposForOwner:
    """Tests for list_repos_for_owner function."""

    @patch("worktrees_hives.discover._run_gh")
    def test_list_repos_success(self, mock_run_gh: MagicMock) -> None:
        """Test successful repo listing."""
        mock_run_gh.return_value = (json.dumps([{"name": "repo1"}, {"name": "repo2"}]), "", 0)

        repos, error = list_repos_for_owner("rmems")

        assert error is None
        assert repos == ["repo1", "repo2"]

    @patch("worktrees_hives.discover._run_gh")
    def test_list_repos_error(self, mock_run_gh: MagicMock) -> None:
        """Test handling of repo listing errors."""
        mock_run_gh.return_value = ("", "Organization not found", 1)

        repos, error = list_repos_for_owner("nonexistent-org")

        assert repos == []
        assert error is not None


class TestDiscoverAll:
    """Tests for discover_all function."""

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    @patch("worktrees_hives.discover.discover_pull_requests_for_repo")
    def test_discover_all_success(
        self,
        mock_discover_prs: MagicMock,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
        sample_pr: Issue,
    ) -> None:
        """Test successful discovery across all owners."""
        mock_list_repos.return_value = (["test-repo"], None)
        mock_discover_issues.return_value = ([sample_issue], None)
        mock_discover_prs.return_value = ([sample_pr], None)

        result = discover_all()

        # Each owner contributes 1 issue + 1 PR = 2 per owner, 2 owners = 4 total
        assert len(result.issues) == 4
        assert len(result.errors) == 0
        assert result.owners_scanned == ALLOWED_OWNERS

    @patch("worktrees_hives.discover.list_repos_for_owner")
    def test_discover_all_repo_listing_error(self, mock_list_repos: MagicMock) -> None:
        """Test handling of repo listing errors in discover_all."""
        mock_list_repos.return_value = ([], "Failed to list repos")

        result = discover_all()

        assert len(result.issues) == 0
        assert len(result.errors) == 2  # One error per owner

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    def test_discover_all_without_prs(
        self,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
    ) -> None:
        """Test discovery without including PRs."""
        mock_list_repos.return_value = (["test-repo"], None)
        mock_discover_issues.return_value = ([sample_issue], None)

        result = discover_all(include_prs=False)

        # Each owner contributes 1 issue, 2 owners = 2 total
        assert len(result.issues) == 2
        assert all(not issue.is_pr for issue in result.issues)

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    def test_discover_all_custom_owners(
        self,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
    ) -> None:
        """Test discovery with custom owners list."""
        mock_list_repos.return_value = (["test-repo"], None)
        mock_discover_issues.return_value = ([sample_issue], None)

        custom_owners = ["custom-owner"]
        result = discover_all(owners=custom_owners)

        assert result.owners_scanned == custom_owners
        mock_list_repos.assert_called_once_with("custom-owner")


class TestFilterIssues:
    """Tests for filter_issues function."""

    def test_filter_by_labels(self, sample_issue: Issue) -> None:
        """Test filtering by labels."""
        filtered = filter_issues([sample_issue], labels=["bug"])

        assert len(filtered) == 1
        assert filtered[0] == sample_issue

    def test_filter_by_labels_not_found(self, sample_issue: Issue) -> None:
        """Test filtering by labels that don't exist."""
        filtered = filter_issues([sample_issue], labels=["nonexistent"])

        assert len(filtered) == 0

    def test_filter_by_milestone(self, sample_issue: Issue) -> None:
        """Test filtering by milestone."""
        filtered = filter_issues([sample_issue], milestone="v1.0")

        assert len(filtered) == 1

    def test_filter_by_milestone_not_found(self, sample_issue: Issue) -> None:
        """Test filtering by milestone that doesn't exist."""
        filtered = filter_issues([sample_issue], milestone="v2.0")

        assert len(filtered) == 0

    def test_filter_by_assignee(self, sample_issue: Issue) -> None:
        """Test filtering by assignee."""
        filtered = filter_issues([sample_issue], assignee="user1")

        assert len(filtered) == 1

    def test_filter_by_assignee_not_found(self, sample_issue: Issue) -> None:
        """Test filtering by assignee that doesn't exist."""
        filtered = filter_issues([sample_issue], assignee="nonexistent")

        assert len(filtered) == 0

    def test_filter_by_is_pr(self, sample_issue: Issue, sample_pr: Issue) -> None:
        """Test filtering by PR status."""
        issues = [sample_issue, sample_pr]

        # Filter for PRs only
        prs = filter_issues(issues, is_pr=True)
        assert len(prs) == 1
        assert prs[0].is_pr is True

        # Filter for issues only
        issues_only = filter_issues(issues, is_pr=False)
        assert len(issues_only) == 1
        assert issues_only[0].is_pr is False

    def test_filter_multiple_criteria(self, sample_issue: Issue) -> None:
        """Test filtering by multiple criteria."""
        filtered = filter_issues(
            [sample_issue],
            labels=["bug"],
            milestone="v1.0",
            assignee="user1",
        )

        assert len(filtered) == 1

    def test_filter_no_criteria(self, sample_issue: Issue) -> None:
        """Test filtering with no criteria returns all issues."""
        filtered = filter_issues([sample_issue])

        assert len(filtered) == 1


class TestFormatForOrchestrator:
    """Tests for format_for_orchestrator function."""

    def test_format_for_orchestrator(self, sample_issue: Issue, sample_pr: Issue) -> None:
        """Test formatting for orchestrator consumption."""
        result = DiscoveryResult(
            issues=[sample_issue, sample_pr],
            errors=["Test error"],
            owners_scanned=["rmems", "Limen-Neural"],
        )

        formatted = format_for_orchestrator(result)

        assert formatted["total_issues"] == 2
        assert formatted["total_errors"] == 1
        assert formatted["owners_scanned"] == ["rmems", "Limen-Neural"]
        assert len(formatted["issues"]) == 2
        assert formatted["errors"] == ["Test error"]

        # Check issue format
        issue_data = formatted["issues"][0]
        assert issue_data["number"] == 42
        assert issue_data["title"] == "Test Issue"
        assert issue_data["state"] == "open"
        assert issue_data["labels"] == ["bug", "priority:high"]
        assert issue_data["milestone"] == "v1.0"
        assert issue_data["is_pr"] is False

    def test_format_for_orchestrator_empty(self) -> None:
        """Test formatting empty result."""
        result = DiscoveryResult(
            issues=[],
            errors=[],
            owners_scanned=[],
        )

        formatted = format_for_orchestrator(result)

        assert formatted["total_issues"] == 0
        assert formatted["total_errors"] == 0
        assert formatted["issues"] == []
        assert formatted["errors"] == []


class TestIssueState:
    """Tests for IssueState enum."""

    def test_issue_state_values(self) -> None:
        """Test IssueState enum values."""
        assert IssueState.OPEN.value == "open"
        assert IssueState.CLOSED.value == "closed"
        assert IssueState.IN_PROGRESS.value == "in_progress"


class TestAllowedOwners:
    """Tests for ALLOWED_OWNERS constant."""

    def test_allowed_owners(self) -> None:
        """Test ALLOWED_OWNERS contains expected owners."""
        assert "rmems" in ALLOWED_OWNERS
        assert "Limen-Neural" in ALLOWED_OWNERS
        assert len(ALLOWED_OWNERS) == 2
