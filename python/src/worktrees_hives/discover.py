"""Discovery module for finding eligible work across allowed GitHub owners."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any


class IssueState(str, Enum):
    """State of a GitHub issue or PR."""
    OPEN = "open"
    CLOSED = "closed"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class Issue:
    """Represents a GitHub issue or PR."""
    number: int
    title: str
    state: str
    labels: list[str]
    milestone: str | None
    url: str
    owner: str
    repo: str
    is_pr: bool
    assignees: list[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Result of discovering issues across allowed owners."""
    issues: list[Issue]
    errors: list[str]
    owners_scanned: list[str]


ALLOWED_OWNERS = ["rmems", "Limen-Neural"]


def _run_gh(args: list[str]) -> tuple[str, str, int]:
    """Run a gh CLI command and return stdout, stderr, returncode."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        return "", "gh CLI not found", 1
    except subprocess.TimeoutExpired:
        return "", "gh command timed out", 1


def _parse_issue(data: dict[str, Any], owner: str, repo: str) -> Issue:
    """Parse a GitHub issue/PR JSON response into an Issue object."""
    labels = [label["name"] for label in data.get("labels", [])]
    milestone_data = data.get("milestone")
    milestone = milestone_data["title"] if milestone_data else None
    assignees = [assignee["login"] for assignee in data.get("assignees", [])]

    return Issue(
        number=data["number"],
        title=data["title"],
        state=data["state"].lower(),
        labels=labels,
        milestone=milestone,
        url=data["html_url"],
        owner=owner,
        repo=repo,
        is_pr="pullRequest" in data or data.get("pull_request") is not None,
        assignees=assignees,
        created_at=data["createdAt"] if "createdAt" in data else data.get("created_at", ""),
        updated_at=data["updatedAt"] if "updatedAt" in data else data.get("updated_at", ""),
    )


def discover_issues_for_repo(
    owner: str,
    repo: str,
    state: IssueState = IssueState.OPEN,
) -> tuple[list[Issue], str | None]:
    """Discover issues for a specific repository.

    Args:
        owner: GitHub owner (org or user)
        repo: Repository name
        state: Filter by issue state

    Returns:
        Tuple of (list of issues, error message or None)
    """
    args = [
        "issue", "list",
        "--repo", f"{owner}/{repo}",
        "--state", state.value,
        "--json", "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
        "--limit", "100",
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        if "Could not resolve to a Repository" in stderr:
            return [], None  # Repo doesn't exist, skip silently
        return [], f"Failed to query {owner}/{repo}: {stderr}"

    try:
        data = json.loads(stdout)
        issues = [_parse_issue(item, owner, repo) for item in data]
        return issues, None
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for {owner}/{repo}: {e}"


def discover_pull_requests_for_repo(
    owner: str,
    repo: str,
    state: str = "open",
) -> tuple[list[Issue], str | None]:
    """Discover pull requests for a specific repository.

    Args:
        owner: GitHub owner (org or user)
        repo: Repository name
        state: Filter by PR state (open, closed, all)

    Returns:
        Tuple of (list of issues representing PRs, error message or None)
    """
    args = [
        "pr", "list",
        "--repo", f"{owner}/{repo}",
        "--state", state,
        "--json", "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
        "--limit", "100",
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        if "Could not resolve to a Repository" in stderr:
            return [], None  # Repo doesn't exist, skip silently
        return [], f"Failed to query PRs for {owner}/{repo}: {stderr}"

    try:
        data = json.loads(stdout)
        issues = []
        for item in data:
            issue = _parse_issue(item, owner, repo)
            # PRs are always marked as PRs
            issues.append(Issue(
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
            ))
        return issues, None
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for PRs in {owner}/{repo}: {e}"


def list_repos_for_owner(owner: str) -> tuple[list[str], str | None]:
    """List repositories for a given owner.

    Args:
        owner: GitHub owner (org or user)

    Returns:
        Tuple of (list of repo names, error message or None)
    """
    args = [
        "repo", "list", owner,
        "--json", "name",
        "--limit", "100",
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        return [], f"Failed to list repos for {owner}: {stderr}"

    try:
        data = json.loads(stdout)
        repos = [item["name"] for item in data]
        return repos, None
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for repos of {owner}: {e}"


def discover_all(
    owners: list[str] | None = None,
    state: IssueState = IssueState.OPEN,
    include_prs: bool = True,
) -> DiscoveryResult:
    """Discover all eligible work across allowed owners.

    Args:
        owners: List of GitHub owners to scan. Defaults to ALLOWED_OWNERS.
        state: Filter by issue state
        include_prs: Whether to also discover pull requests

    Returns:
        DiscoveryResult with all found issues and any errors
    """
    if owners is None:
        owners = ALLOWED_OWNERS

    all_issues: list[Issue] = []
    all_errors: list[str] = []

    for owner in owners:
        # List repos for this owner
        repos, error = list_repos_for_owner(owner)
        if error:
            all_errors.append(error)
            continue

        for repo in repos:
            # Discover issues
            issues, error = discover_issues_for_repo(owner, repo, state)
            if error:
                all_errors.append(error)
            else:
                all_issues.extend(issues)

            # Discover PRs if requested
            if include_prs:
                prs, error = discover_pull_requests_for_repo(owner, repo, state.value)
                if error:
                    all_errors.append(error)
                else:
                    all_issues.extend(prs)

    return DiscoveryResult(
        issues=all_issues,
        errors=all_errors,
        owners_scanned=owners,
    )


def filter_issues(
    issues: list[Issue],
    labels: list[str] | None = None,
    milestone: str | None = None,
    assignee: str | None = None,
    is_pr: bool | None = None,
) -> list[Issue]:
    """Filter issues by various criteria.

    Args:
        issues: List of issues to filter
        labels: Filter by labels (must have all specified labels)
        milestone: Filter by milestone title
        assignee: Filter by assignee username
        is_pr: Filter by whether it's a PR

    Returns:
        Filtered list of issues
    """
    result = issues

    if labels:
        result = [
            issue for issue in result
            if all(label in issue.labels for label in labels)
        ]

    if milestone is not None:
        result = [
            issue for issue in result
            if issue.milestone == milestone
        ]

    if assignee is not None:
        result = [
            issue for issue in result
            if assignee in issue.assignees
        ]

    if is_pr is not None:
        result = [
            issue for issue in result
            if issue.is_pr == is_pr
        ]

    return result


def format_for_orchestrator(result: DiscoveryResult) -> dict[str, Any]:
    """Format discovery result for orchestrator consumption.

    Args:
        result: DiscoveryResult to format

    Returns:
        Dictionary suitable for orchestrator consumption
    """
    return {
        "total_issues": len(result.issues),
        "total_errors": len(result.errors),
        "owners_scanned": result.owners_scanned,
        "issues": [
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "labels": issue.labels,
                "milestone": issue.milestone,
                "url": issue.url,
                "owner": issue.owner,
                "repo": issue.repo,
                "is_pr": issue.is_pr,
                "assignees": issue.assignees,
                "created_at": issue.created_at,
                "updated_at": issue.updated_at,
            }
            for issue in result.issues
        ],
        "errors": result.errors,
    }
