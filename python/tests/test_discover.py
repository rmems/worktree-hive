"""Tests for the discovery module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.discover import (
    ALLOWED_OWNERS,
    DISCOVERY_ITEM_LIMIT,
    DiscoveryResult,
    Issue,
    IssueState,
    OwnerPolicyError,
    _parse_issue,
    discover_all,
    discover_issues_for_repo,
    discover_pull_requests_for_repo,
    filter_issues,
    format_for_orchestrator,
    list_repos_for_owner,
    load_allowed_owners_from_env,
)


@pytest.fixture
def sample_issue_data() -> dict:
    """Sample GitHub issue JSON data (generic owner)."""
    return {
        "number": 42,
        "title": "Test Issue",
        "state": "OPEN",
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "milestone": {"title": "v1.0"},
        "html_url": "https://github.com/acme/test-repo/issues/42",
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
        "html_url": "https://github.com/acme/test-repo/pull/10",
        "assignees": [{"login": "user1"}],
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-03T00:00:00Z",
        "pullRequest": {"url": "https://api.github.com/repos/acme/test-repo/pulls/10"},
    }


@pytest.fixture
def sample_issue(sample_issue_data: dict) -> Issue:
    """Sample Issue object."""
    return _parse_issue(sample_issue_data, "acme", "test-repo")


@pytest.fixture
def sample_pr(sample_pr_data: dict) -> Issue:
    """Sample Issue object representing a PR."""
    issue = _parse_issue(sample_pr_data, "acme", "test-repo")
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
        issue = _parse_issue(sample_issue_data, "acme", "test-repo")

        assert issue.number == 42
        assert issue.title == "Test Issue"
        assert issue.state == "open"
        assert issue.labels == ["bug", "priority:high"]
        assert issue.milestone == "v1.0"
        assert issue.url == "https://github.com/acme/test-repo/issues/42"
        assert issue.owner == "acme"
        assert issue.repo == "test-repo"
        assert issue.is_pr is False
        assert issue.assignees == ["user1", "user2"]
        assert issue.created_at == "2025-01-01T00:00:00Z"
        assert issue.updated_at == "2025-01-02T00:00:00Z"

    def test_parse_issue_no_milestone(self, sample_issue_data: dict) -> None:
        """Test parsing an issue without a milestone."""
        sample_issue_data["milestone"] = None
        issue = _parse_issue(sample_issue_data, "acme", "test-repo")

        assert issue.milestone is None

    def test_parse_issue_no_labels(self, sample_issue_data: dict) -> None:
        """Test parsing an issue without labels."""
        sample_issue_data["labels"] = []
        issue = _parse_issue(sample_issue_data, "acme", "test-repo")

        assert issue.labels == []

    def test_parse_issue_no_assignees(self, sample_issue_data: dict) -> None:
        """Test parsing an issue without assignees."""
        sample_issue_data["assignees"] = []
        issue = _parse_issue(sample_issue_data, "acme", "test-repo")

        assert issue.assignees == []

    def test_parse_issue_with_pull_request(self, sample_pr_data: dict) -> None:
        """Test parsing a pull request."""
        issue = _parse_issue(sample_pr_data, "acme", "test-repo")

        assert issue.is_pr is True

    def test_parse_malformed_labels_soft(self, sample_issue_data: dict) -> None:
        """Malformed label entries are skipped, not fatal."""
        sample_issue_data["labels"] = [{"name": "ok"}, "string-label", {"no_name": True}, None]
        issue = _parse_issue(sample_issue_data, "acme", "test-repo")
        assert "ok" in issue.labels
        assert "string-label" in issue.labels


class TestDiscoverIssuesForRepo:
    """Tests for discover_issues_for_repo function."""

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_success(self, mock_run_gh: MagicMock, sample_issue_data: dict) -> None:
        """Test successful issue discovery."""
        mock_run_gh.return_value = (json.dumps([sample_issue_data]), "", 0)

        issues, error, truncated = discover_issues_for_repo("acme", "test-repo")

        assert error is None
        assert truncated is False
        assert len(issues) == 1
        assert issues[0].number == 42
        mock_run_gh.assert_called_once_with(
            [
                "issue",
                "list",
                "--repo",
                "acme/test-repo",
                "--state",
                "open",
                "--json",
                "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
                "--limit",
                "1000",
            ]
        )

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_repo_not_found(self, mock_run_gh: MagicMock) -> None:
        """Test handling of non-existent repository."""
        mock_run_gh.return_value = ("", "Could not resolve to a Repository", 1)

        issues, error, truncated = discover_issues_for_repo("acme", "nonexistent-repo")

        assert issues == []
        assert error is None
        assert truncated is False

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_other_error(self, mock_run_gh: MagicMock) -> None:
        """Test handling of other errors."""
        mock_run_gh.return_value = ("", "Authentication failed", 1)

        issues, error, truncated = discover_issues_for_repo("acme", "test-repo")

        assert issues == []
        assert error is not None
        assert "Authentication failed" in error
        assert truncated is False

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_json_parse_error(self, mock_run_gh: MagicMock) -> None:
        """Test handling of JSON parse errors."""
        mock_run_gh.return_value = ("invalid json", "", 0)

        issues, error, truncated = discover_issues_for_repo("acme", "test-repo")

        assert issues == []
        assert error is not None
        assert "Failed to parse JSON" in error

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_issues_with_state_filter(
        self, mock_run_gh: MagicMock, sample_issue_data: dict
    ) -> None:
        """Test issue discovery with state filter."""
        mock_run_gh.return_value = (json.dumps([sample_issue_data]), "", 0)

        issues, error, truncated = discover_issues_for_repo("acme", "test-repo", IssueState.CLOSED)

        assert error is None
        mock_run_gh.assert_called_once_with(
            [
                "issue",
                "list",
                "--repo",
                "acme/test-repo",
                "--state",
                "closed",
                "--json",
                "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
                "--limit",
                "1000",
            ]
        )

    @patch("worktrees_hives.discover._run_gh")
    def test_soft_parse_one_bad_item(self, mock_run_gh: MagicMock, sample_issue_data: dict) -> None:
        """One bad list item does not abort the whole repo parse."""
        bad = {"title": "no number"}
        mock_run_gh.return_value = (json.dumps([bad, sample_issue_data]), "", 0)

        issues, error, truncated = discover_issues_for_repo("acme", "test-repo")

        assert len(issues) == 1
        assert issues[0].number == 42
        assert error is not None
        assert "Skipped malformed" in error
        assert truncated is False

    @patch("worktrees_hives.discover._run_gh")
    def test_truncated_when_at_limit(self, mock_run_gh: MagicMock) -> None:
        """Hitting DISCOVERY_ITEM_LIMIT sets truncated=True."""
        items = [
            {
                "number": i,
                "title": f"I{i}",
                "state": "OPEN",
                "labels": [],
                "milestone": None,
                "url": f"https://example.com/{i}",
                "assignees": [],
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-01T00:00:00Z",
            }
            for i in range(DISCOVERY_ITEM_LIMIT)
        ]
        mock_run_gh.return_value = (json.dumps(items), "", 0)

        issues, error, truncated = discover_issues_for_repo("acme", "test-repo")

        assert len(issues) == DISCOVERY_ITEM_LIMIT
        assert truncated is True
        assert error is not None
        assert "DISCOVERY_ITEM_LIMIT" in error


class TestDiscoverPullRequestsForRepo:
    """Tests for discover_pull_requests_for_repo function."""

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_prs_success(self, mock_run_gh: MagicMock, sample_pr_data: dict) -> None:
        """Test successful PR discovery."""
        mock_run_gh.return_value = (json.dumps([sample_pr_data]), "", 0)

        prs, error, truncated = discover_pull_requests_for_repo("acme", "test-repo")

        assert error is None
        assert truncated is False
        assert len(prs) == 1
        assert prs[0].number == 10
        assert prs[0].is_pr is True

    @patch("worktrees_hives.discover._run_gh")
    def test_discover_prs_repo_not_found(self, mock_run_gh: MagicMock) -> None:
        """Test handling of non-existent repository for PRs."""
        mock_run_gh.return_value = ("", "Could not resolve to a Repository", 1)

        prs, error, truncated = discover_pull_requests_for_repo("acme", "nonexistent-repo")

        assert prs == []
        assert error is None
        assert truncated is False


class TestListReposForOwner:
    """Tests for list_repos_for_owner function."""

    @patch("worktrees_hives.discover._run_gh")
    def test_list_repos_success(self, mock_run_gh: MagicMock) -> None:
        """Test successful repo listing."""
        mock_run_gh.return_value = (
            json.dumps([{"name": "repo1"}, {"name": "repo2"}]),
            "",
            0,
        )

        repos, error, truncated = list_repos_for_owner("acme")

        assert error is None
        assert repos == ["repo1", "repo2"]
        assert truncated is False

    @patch("worktrees_hives.discover._run_gh")
    def test_list_repos_error(self, mock_run_gh: MagicMock) -> None:
        """Test handling of repo listing errors."""
        mock_run_gh.return_value = ("", "Organization not found", 1)

        repos, error, truncated = list_repos_for_owner("nonexistent-org")

        assert repos == []
        assert error is not None
        assert truncated is False


class TestDiscoverAll:
    """Tests for discover_all function."""

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    @patch("worktrees_hives.discover.discover_pull_requests_for_repo")
    def test_discover_all_with_explicit_owners(
        self,
        mock_discover_prs: MagicMock,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
        sample_pr: Issue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit owners list drives a non-empty scan when env allowlist is empty."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        mock_list_repos.return_value = (["test-repo"], None, False)
        mock_discover_issues.return_value = ([sample_issue], None, False)
        mock_discover_prs.return_value = ([sample_pr], None, False)

        result = discover_all(
            owners=["acme", "example-org"],
            check_auth=False,
        )

        # Each owner contributes 1 issue + 1 PR = 2 per owner, 2 owners = 4 total
        assert len(result.issues) == 4
        assert result.truncated is False
        assert result.owners_scanned == ["acme", "example-org"]

    @patch("worktrees_hives.discover.list_repos_for_owner")
    def test_discover_all_repo_listing_error(
        self, mock_list_repos: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test handling of repo listing errors in discover_all."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        mock_list_repos.return_value = ([], "Failed to list repos", False)

        result = discover_all(owners=["acme", "example-org"], check_auth=False)

        assert len(result.issues) == 0
        assert len(result.errors) == 2  # One error per owner

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    def test_discover_all_without_prs(
        self,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test discovery without including PRs."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        mock_list_repos.return_value = (["test-repo"], None, False)
        mock_discover_issues.return_value = ([sample_issue], None, False)

        result = discover_all(
            owners=["acme", "example-org"],
            include_prs=False,
            check_auth=False,
        )

        assert len(result.issues) == 2
        assert all(not issue.is_pr for issue in result.issues)

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    def test_discover_all_custom_owners(
        self,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test discovery with custom owners list."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        mock_list_repos.return_value = (["test-repo"], None, False)
        mock_discover_issues.return_value = ([sample_issue], None, False)

        custom_owners = ["custom-owner"]
        result = discover_all(owners=custom_owners, check_auth=False)

        assert result.owners_scanned == custom_owners
        mock_list_repos.assert_called_once_with("custom-owner")

    def test_discover_all_empty_default_no_owners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty default (no env, no owners=) yields empty scan with guidance error."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        result = discover_all(check_auth=False)
        assert result.issues == []
        assert result.owners_scanned == []
        assert any("WH_ALLOWED_OWNERS" in e for e in result.errors)

    @patch("worktrees_hives.discover.list_repos_for_owner")
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    @patch("worktrees_hives.discover.discover_pull_requests_for_repo")
    def test_discover_all_from_env_allowlist(
        self,
        mock_discover_prs: MagicMock,
        mock_discover_issues: MagicMock,
        mock_list_repos: MagicMock,
        sample_issue: Issue,
        sample_pr: Issue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WH_ALLOWED_OWNERS supplies default owners when owners= is omitted."""
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme,example-org")
        mock_list_repos.return_value = (["test-repo"], None, False)
        mock_discover_issues.return_value = ([sample_issue], None, False)
        mock_discover_prs.return_value = ([sample_pr], None, False)

        result = discover_all(check_auth=False)

        assert result.owners_scanned == ["acme", "example-org"]
        assert len(result.issues) == 4


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

        prs = filter_issues(issues, is_pr=True)
        assert len(prs) == 1
        assert prs[0].is_pr is True

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
            owners_scanned=["acme", "example-org"],
            truncated=True,
        )

        formatted = format_for_orchestrator(result)

        assert formatted["total_issues"] == 2
        assert formatted["total_errors"] == 1
        assert formatted["owners_scanned"] == ["acme", "example-org"]
        assert formatted["truncated"] is True
        assert len(formatted["issues"]) == 2
        assert formatted["errors"] == ["Test error"]

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
        assert formatted["truncated"] is False


class TestIssueState:
    """Tests for IssueState enum."""

    def test_issue_state_values(self) -> None:
        """Test IssueState enum values."""
        assert IssueState.OPEN.value == "open"
        assert IssueState.CLOSED.value == "closed"


class TestAllowedOwners:
    """Tests for empty default allowlist + env resolution."""

    def test_default_allowed_owners_empty(self) -> None:
        """Module default allowlist is empty (no org hardcoding)."""
        assert ALLOWED_OWNERS == []
        assert len(ALLOWED_OWNERS) == 0

    def test_load_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme, example-org")
        assert load_allowed_owners_from_env() == ["acme", "example-org"]

    def test_load_from_env_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        assert load_allowed_owners_from_env() == []


class TestOwnerPolicy:
    """Owner allowlist and override behavior."""

    def test_reject_owners_outside_env_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        with pytest.raises(OwnerPolicyError, match="allow_non_default_owners"):
            discover_all(owners=["evil-corp"], check_auth=False)

    def test_allow_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        with (
            patch(
                "worktrees_hives.discover.list_repos_for_owner",
                return_value=([], None, False),
            ),
        ):
            result = discover_all(
                owners=["evil-corp"],
                allow_non_default_owners=True,
                check_auth=False,
            )
        assert result.owners_scanned == ["evil-corp"]

    def test_owners_scanned_is_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        with (
            patch(
                "worktrees_hives.discover.list_repos_for_owner",
                return_value=([], None, False),
            ),
            patch("worktrees_hives.discover.ensure_gh_auth", return_value=None),
        ):
            result = discover_all(check_auth=True)
        assert result.owners_scanned == ["acme"]
        result.owners_scanned.append("mutated")
        # env-resolved list should not be shared with later loads
        assert load_allowed_owners_from_env() == ["acme"]


class TestAuthFailFast:
    @patch(
        "worktrees_hives.discover.ensure_gh_auth",
        return_value="GitHub CLI authentication failed",
    )
    def test_auth_failure_short_circuits(
        self, mock_auth: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        result = discover_all(check_auth=True)
        assert result.issues == []
        assert any("authentication failed" in e.lower() for e in result.errors)


class TestPrOnlyMode:
    @patch("worktrees_hives.discover.list_repos_for_owner", return_value=(["r"], None, False))
    @patch("worktrees_hives.discover.discover_issues_for_repo")
    @patch("worktrees_hives.discover.discover_pull_requests_for_repo")
    def test_kind_prs_skips_issues(
        self,
        mock_prs: MagicMock,
        mock_issues: MagicMock,
        mock_repos: MagicMock,
        sample_pr: Issue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        mock_prs.return_value = ([sample_pr], None, False)
        result = discover_all(owners=["acme"], kind="prs", check_auth=False)
        mock_issues.assert_not_called()
        mock_prs.assert_called()
        assert all(i.is_pr for i in result.issues)


class TestCiRollup:
    def test_parse_pr_ci_status(self) -> None:
        data = {
            "number": 1,
            "title": "PR",
            "state": "OPEN",
            "labels": [],
            "milestone": None,
            "url": "https://example.com",
            "assignees": [],
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T00:00:00Z",
            "pullRequest": {},
            "statusCheckRollup": [
                {"name": "ci", "state": "SUCCESS", "conclusion": "SUCCESS"},
            ],
        }
        issue = _parse_issue(data, "acme", "repo")
        assert issue.ci_status == "success"
