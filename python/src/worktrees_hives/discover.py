"""Discovery module for finding eligible work across allowed GitHub owners."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Literal

# Soft per-resource fetch cap. gh list commands paginate internally up to --limit.
DISCOVERY_ITEM_LIMIT = 1000

DiscoveryKind = Literal["all", "issues", "prs"]


class IssueState(str, Enum):
    """State of a GitHub issue or PR.

    GitHub's issue/PR list APIs only support open, closed, and all.
    An "in_progress" workflow state is not a first-class GitHub issue state.
    """

    OPEN = "open"
    CLOSED = "closed"


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
    ci_status: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """Result of discovering issues across allowed owners."""

    issues: list[Issue]
    errors: list[str]
    owners_scanned: list[str]


ALLOWED_OWNERS = ["rmems", "Limen-Neural"]
ALLOWED_OWNERS_SET = frozenset(ALLOWED_OWNERS)


class OwnerPolicyError(ValueError):
    """Raised when discovery is asked to scan a non-allowlisted owner without override."""


def _run_gh(args: list[str]) -> tuple[str, str, int]:
    """Run a gh CLI command and return stdout, stderr, returncode."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        return "", "gh CLI not found", 1
    except OSError as e:
        return "", f"Failed to execute gh command: {e}", 1
    except subprocess.TimeoutExpired:
        return "", "gh command timed out", 1


def ensure_gh_auth() -> str | None:
    """Fail fast when gh authentication is missing or broken.

    Returns an error message when auth is unusable, else None.
    """
    stdout, stderr, returncode = _run_gh(["auth", "status"])
    if returncode == 0:
        return None
    detail = (stderr or stdout or "gh auth status failed").strip()
    return (
        "GitHub CLI authentication failed. Run `gh auth login` (or refresh the token) "
        f"before discovery. Detail: {detail}"
    )


def _summarize_ci_rollup(rollup: Any) -> str | None:
    """Reduce statusCheckRollup to success|failure|pending|neutral|unknown."""
    if rollup is None:
        return None
    if not isinstance(rollup, list) or not rollup:
        return "unknown"

    has_failure = False
    has_pending = False
    has_success = False
    for item in rollup:
        if not isinstance(item, dict):
            continue
        conclusion = (item.get("conclusion") or "").upper()
        state = (item.get("state") or item.get("status") or "").upper()
        if conclusion in (
            "FAILURE",
            "ERROR",
            "ACTION_REQUIRED",
            "TIMED_OUT",
            "CANCELLED",
        ) or state in (
            "FAILURE",
            "ERROR",
            "CANCELLED",
        ):
            has_failure = True
        elif state in (
            "IN_PROGRESS",
            "QUEUED",
            "PENDING",
            "WAITING",
            "EXPECTED",
            "REQUESTED",
        ) or (conclusion in ("", "NONE") and state not in ("SUCCESS", "COMPLETED", "SKIPPED")):
            # Pending / incomplete check
            if conclusion not in ("SUCCESS", "SKIPPED", "NEUTRAL"):
                has_pending = True
        elif conclusion == "SUCCESS" or state == "SUCCESS":
            has_success = True

    if has_failure:
        return "failure"
    if has_pending:
        return "pending"
    if has_success:
        return "success"
    return "neutral"


def _parse_issue(data: dict[str, Any], owner: str, repo: str) -> Issue:
    """Parse a GitHub issue/PR JSON response into an Issue object."""
    labels = [label["name"] for label in data.get("labels", [])]
    milestone_data = data.get("milestone")
    milestone = milestone_data.get("title") if milestone_data else None
    assignees = [assignee["login"] for assignee in data.get("assignees", [])]
    ci_status = _summarize_ci_rollup(data.get("statusCheckRollup"))

    try:
        return Issue(
            number=data["number"],
            title=data["title"],
            state=data["state"].lower(),
            labels=labels,
            milestone=milestone,
            url=data.get("url", data.get("html_url", "")),
            owner=owner,
            repo=repo,
            is_pr="pullRequest" in data or data.get("pull_request") is not None,
            assignees=assignees,
            created_at=data["createdAt"] if "createdAt" in data else data.get("created_at", ""),
            updated_at=data["updatedAt"] if "updatedAt" in data else data.get("updated_at", ""),
            ci_status=ci_status,
        )
    except KeyError as e:
        raise ValueError(f"Missing required field in issue data: {e}") from e


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
        "issue",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--state",
        state.value,
        "--json",
        "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
        "--limit",
        str(DISCOVERY_ITEM_LIMIT),
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

    Includes CI rollup (`statusCheckRollup`) when available from gh pr list.

    Args:
        owner: GitHub owner (org or user)
        repo: Repository name
        state: Filter by PR state (open, closed, all)

    Returns:
        Tuple of (list of issues representing PRs, error message or None)
    """
    args = [
        "pr",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--state",
        state,
        "--json",
        "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt,statusCheckRollup",
        "--limit",
        str(DISCOVERY_ITEM_LIMIT),
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
            issues.append(replace(issue, is_pr=True))
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
        "repo",
        "list",
        owner,
        "--json",
        "name",
        "--limit",
        str(DISCOVERY_ITEM_LIMIT),
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        return [], f"Failed to list repos for {owner}: {stderr}"

    try:
        data = json.loads(stdout)
        repos = []
        for item in data:
            if "name" not in item:
                continue  # Skip malformed items
            repos.append(item["name"])
        return repos, None
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for repos of {owner}: {e}"


def _resolve_owners(
    owners: list[str] | None,
    *,
    allow_non_default_owners: bool,
) -> list[str]:
    """Resolve and validate the owner scan list.

    Non-default owners require an explicit override flag (AGENTS.md scope rule).
    """
    if owners is None:
        return list(ALLOWED_OWNERS)

    resolved = list(owners)
    disallowed = [o for o in resolved if o not in ALLOWED_OWNERS_SET]
    if disallowed and not allow_non_default_owners:
        raise OwnerPolicyError(
            "Owner(s) not in default allowlist "
            f"{sorted(ALLOWED_OWNERS_SET)}: {disallowed}. "
            "Pass allow_non_default_owners=True for an explicit override."
        )
    return resolved


def discover_all(
    owners: list[str] | None = None,
    state: IssueState = IssueState.OPEN,
    include_prs: bool = True,
    include_issues: bool = True,
    kind: DiscoveryKind | None = None,
    allow_non_default_owners: bool = False,
    check_auth: bool = True,
) -> DiscoveryResult:
    """Discover all eligible work across allowed owners.

    Args:
        owners: List of GitHub owners to scan. Defaults to ALLOWED_OWNERS.
        state: Filter by issue state
        include_prs: Whether to also discover pull requests (ignored if kind set)
        include_issues: Whether to discover issues (ignored if kind set)
        kind: High-level mode: "all" | "issues" | "prs". When set, overrides
            include_prs/include_issues for orchestrator kind=prs|issues|all.
        allow_non_default_owners: Explicit override to scan owners outside the
            default allowlist (rmems, Limen-Neural).
        check_auth: When True, fail fast if `gh auth status` is broken.

    Returns:
        DiscoveryResult with all found issues and any errors
    """
    if kind == "prs":
        include_issues, include_prs = False, True
    elif kind == "issues":
        include_issues, include_prs = True, False
    elif kind == "all":
        include_issues, include_prs = True, True

    if not include_issues and not include_prs:
        raise ValueError("At least one of include_issues or include_prs must be True")

    owners = _resolve_owners(owners, allow_non_default_owners=allow_non_default_owners)

    all_issues: list[Issue] = []
    all_errors: list[str] = []

    if check_auth:
        auth_error = ensure_gh_auth()
        if auth_error:
            return DiscoveryResult(
                issues=[],
                errors=[auth_error],
                owners_scanned=list(owners),
            )

    for owner in owners:
        # List repos for this owner
        repos, error = list_repos_for_owner(owner)
        if error:
            all_errors.append(error)
            continue

        for repo in repos:
            if include_issues:
                issues, error = discover_issues_for_repo(owner, repo, state)
                if error:
                    all_errors.append(error)
                else:
                    all_issues.extend(issues)

            if include_prs:
                prs, error = discover_pull_requests_for_repo(owner, repo, state.value)
                if error:
                    all_errors.append(error)
                else:
                    all_issues.extend(prs)

    return DiscoveryResult(
        issues=all_issues,
        errors=all_errors,
        # Copy so callers cannot mutate the module-level allowlist via the result.
        owners_scanned=list(owners),
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
        result = [issue for issue in result if all(label in issue.labels for label in labels)]

    if milestone is not None:
        result = [issue for issue in result if issue.milestone == milestone]

    if assignee is not None:
        result = [issue for issue in result if assignee in issue.assignees]

    if is_pr is not None:
        result = [issue for issue in result if issue.is_pr == is_pr]

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
        "owners_scanned": list(result.owners_scanned),
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
                "ci_status": issue.ci_status,
            }
            for issue in result.issues
        ],
        "errors": result.errors,
    }
