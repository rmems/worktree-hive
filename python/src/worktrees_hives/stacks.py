"""Stack-aware PR ordering for worktrees-hives.

Detects stacked PRs via GitHub base/head chain walks and provides bottom-up
ordering for the orchestrator. Children are deferred while their base is
blocked, and re-evaluated after base changes.

No Graphite — plain git + optional gh-stack only.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StackType(Enum):
    """How the stack was detected."""

    GITHUB = "github"  # gh-stack extension available
    PLAIN = "plain"  # plain git + API chain walk


class PRState(Enum):
    """Simplified PR state for scheduling decisions."""

    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PRInfo:
    """Minimal PR information from GitHub API.

    ``head_owner`` / ``head_repo`` identify the repository that holds the PR
    head branch. For same-repo PRs they match ``owner`` / ``repo``; for fork
    PRs they point at the fork. Parent matching uses this so a fork head ref
    is never treated as a stack parent of a base-repo PR that shares a name.
    """

    number: int
    head_ref: str
    base_ref: str
    repo: str
    owner: str
    state: PRState = PRState.OPEN
    head_owner: str | None = None
    head_repo: str | None = None

    @property
    def key(self) -> str:
        """Unique key for dedup: owner/repo#number."""
        return f"{self.owner}/{self.repo}#{self.number}"

    def head_lives_in_base_repo(self) -> bool:
        """True when the head branch is in the PR's base repository (not a fork).

        ``head_owner``/``head_repo`` of ``None`` means "not provided" and is
        treated as same-repo for backwards-compatible test construction.
        Empty strings mean unknown/deleted head repo and never parent.
        """
        if self.head_owner is None and self.head_repo is None:
            return True
        if not self.head_owner or not self.head_repo:
            return False
        return self.head_owner == self.owner and self.head_repo == self.repo


@dataclass(slots=True)
class StackMember:
    """A PR's position within a stack."""

    pr: PRInfo
    stack_position: int  # 0 = bottom (base), increment toward tips
    is_base: bool  # True if this PR targets the default branch or external ref
    children: list[int] = field(default_factory=list)  # PR numbers of children
    parent: int | None = None  # PR number of parent (None if base)

    def parent_health_key(self) -> str | None:
        """Stable stack_health key for this member's parent (``owner/repo#n``)."""
        if self.parent is None:
            return None
        return f"{self.pr.owner}/{self.pr.repo}#{self.parent}"

    def _is_blocked(self, stack_health: dict[str, PRState] | None = None) -> bool:
        """True if this member's *immediate* parent isn't healthy.

        This is a private immediate-parent helper. Scheduling decisions must use
        :meth:`Stack.get_processable`, which walks the full ancestor chain.
        """
        if self.parent is None:
            return False
        if stack_health is None:
            return True
        parent_key = self.parent_health_key()
        assert parent_key is not None
        parent_state = stack_health.get(parent_key, PRState.UNKNOWN)
        return parent_state not in (PRState.OPEN, PRState.MERGED)

    def _can_process(self, stack_health: dict[str, PRState]) -> bool:
        """Check if this member's *immediate* parent is healthy.

        This is a private immediate-parent helper. Full-chain processability for
        scheduling is implemented by :meth:`Stack.get_processable` /
        :meth:`Stack._is_ancestor_chain_healthy`.

        A member can process (parent-local check) if:
        - It has no parent (is base), OR
        - Its parent is healthy (OPEN or MERGED)
        """
        if self.parent is None:
            return True
        parent_key = self.parent_health_key()
        assert parent_key is not None
        parent_state = stack_health.get(parent_key, PRState.UNKNOWN)
        return parent_state in (PRState.OPEN, PRState.MERGED)


@dataclass(slots=True)
class Stack:
    """A connected chain of stacked PRs."""

    stack_id: str
    repo: str
    owner: str
    members: list[StackMember] = field(default_factory=list)
    stack_type: StackType = StackType.PLAIN
    default_branch: str = "main"
    is_cyclic: bool = False

    @property
    def is_empty(self) -> bool:
        return len(self.members) == 0

    @property
    def bottom(self) -> StackMember | None:
        """The bottom-most member (stack_position == 0)."""
        for m in self.members:
            if m.stack_position == 0:
                return m
        return None

    @property
    def ordered_prs(self) -> list[PRInfo]:
        """PRs in bottom-up processing order."""
        return [m.pr for m in sorted(self.members, key=lambda m: m.stack_position)]

    def get_member(self, pr_number: int) -> StackMember | None:
        """Find a member by PR number."""
        for m in self.members:
            if m.pr.number == pr_number:
                return m
        return None

    def _is_ancestor_chain_healthy(self, pr_number: int, stack_health: dict[str, PRState]) -> bool:
        """Walk the ancestor chain to check if all parents are healthy.

        A member is processable only if every ancestor up to the base is healthy.
        ``stack_health`` is keyed by ``owner/repo#n`` (see :attr:`PRInfo.key`).
        """
        visited: set[int] = set()
        current = pr_number
        while current is not None:
            if current in visited:
                return False  # cycle
            visited.add(current)
            member = self.get_member(current)
            if member is None or member.parent is None:
                return True  # reached base
            parent_key = member.parent_health_key()
            assert parent_key is not None
            parent_state = stack_health.get(parent_key, PRState.UNKNOWN)
            if parent_state not in (PRState.OPEN, PRState.MERGED):
                return False
            current = member.parent
        return True

    def get_processable(self, stack_health: dict[str, PRState]) -> list[PRInfo]:
        """Return PRs that can be processed now (full ancestor chain healthy)."""
        if self.is_cyclic:
            return []
        result = []
        for m in sorted(self.members, key=lambda m: m.stack_position):
            if m.pr.state != PRState.OPEN:
                continue
            if self._is_ancestor_chain_healthy(m.pr.number, stack_health):
                result.append(m.pr)
        return result

    def get_blocked(self, stack_health: dict[str, PRState]) -> list[PRInfo]:
        """Return PRs blocked by unhealthy ancestors."""
        result = []
        for m in self.members:
            if not self._is_ancestor_chain_healthy(m.pr.number, stack_health):
                result.append(m.pr)
        return result


_GH_PR_PAGE_SIZE = 100

# Empty by default — configure via WH_ALLOWED_OWNERS or function args.
DEFAULT_ALLOWED_OWNERS: frozenset[str] = frozenset()

_WH_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"


def load_allowed_owners_from_env(env: dict[str, str] | None = None) -> frozenset[str]:
    """Parse ``WH_ALLOWED_OWNERS`` (comma-separated owner names) into a frozenset.

    Returns an empty set when the variable is unset or blank.
    """
    source = env if env is not None else os.environ
    raw = source.get(_WH_ALLOWED_OWNERS_ENV, "")
    if not raw or not raw.strip():
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def resolve_allowed_owners(allowed_owners: set[str] | frozenset[str] | None = None) -> set[str]:
    """Resolve the effective owner allowlist.

    Precedence:
    1. Explicit ``allowed_owners`` argument (including empty set).
    2. ``WH_ALLOWED_OWNERS`` environment variable when set/non-empty.
    3. :data:`DEFAULT_ALLOWED_OWNERS` (empty frozenset).
    """
    if allowed_owners is not None:
        return set(allowed_owners)
    env_owners = load_allowed_owners_from_env()
    if env_owners:
        return set(env_owners)
    return set(DEFAULT_ALLOWED_OWNERS)


def _parse_github_pr_state(
    raw_state: str,
    merged_at: Any = None,
    mergeable: Any = None,
) -> PRState:
    """Map GitHub PR state strings to PRState.

    A closed PR that carries a non-empty ``merged_at`` timestamp is treated as
    merged because GitHub's REST API keeps ``state`` as ``closed`` for merged
    pull requests.

    When ``mergeable`` is ``"CONFLICTING"`` (gh JSON) or ``False`` (REST bool),
    an otherwise-open PR is treated as :attr:`PRState.CONFLICTING` so stack
    health does not trust conflicted ancestors as processable OPEN parents.
    """
    raw_upper = raw_state.upper()
    if raw_upper == "OPEN":
        if mergeable is False or (
            isinstance(mergeable, str) and mergeable.upper() == "CONFLICTING"
        ):
            return PRState.CONFLICTING
        return PRState.OPEN
    if raw_upper in ("CLOSED", "MERGED"):
        if raw_upper == "MERGED" or bool(merged_at and str(merged_at).strip()):
            return PRState.MERGED
        return PRState.CLOSED
    return PRState.UNKNOWN


class StackDetector:
    """Detects and builds stacks from GitHub PR data.

    Uses API chain walk on baseRefName/headRefName to find connected PRs.
    No Graphite dependency — plain git + optional gh-stack.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        default_branch: str = "main",
        gh_bin: str = "gh",
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch
        self._gh_bin = gh_bin

    def detect_stacks(self, prs: list[PRInfo]) -> list[Stack]:
        """Build stacks from a list of open PRs.

        Groups by ``(owner, repo)`` first so PR numbers never collide across
        repositories, then runs chain detection per repository.
        """
        if not prs:
            return []

        groups: dict[tuple[str, str], list[PRInfo]] = {}
        for pr in prs:
            groups.setdefault((pr.owner, pr.repo), []).append(pr)

        stacks: list[Stack] = []
        for group in groups.values():
            stacks.extend(self._detect_stacks_single_repo(group))
        return stacks

    def _detect_stacks_single_repo(self, prs: list[PRInfo]) -> list[Stack]:
        """Build stacks for PRs that already share one owner/repo.

        Algorithm:
        1. Build parent/children maps from base/head refs
        2. Find all connected components (chains)
        3. Assign stack_id, stack_position bottom-up
        4. Singleton PRs (no parent, no children) get stack_id=None
        """
        if not prs:
            return []

        # Build lookup: headRefName -> list[PRInfo]
        # Exclude default-branch heads as parent candidates (forks often reuse
        # "main"/"master" as head names, which would falsely parent all PRs
        # targeting the default branch).
        # Only heads that live in the base repository can parent a child whose
        # base_ref points at that branch in the base repo.
        head_map: dict[str, list[PRInfo]] = {}
        for pr in prs:
            if pr.head_ref == self.default_branch:
                continue
            if not pr.head_lives_in_base_repo():
                continue
            head_map.setdefault(pr.head_ref, []).append(pr)

        # Number -> PRInfo for O(1) lookups (safe: single-repo group)
        pr_map: dict[int, PRInfo] = {pr.number: pr for pr in prs}

        # Build parent/children relationships
        parent_of: dict[int, int | None] = {}
        children_of: dict[int, list[int]] = {pr.number: [] for pr in prs}

        for pr in prs:
            # Find parent: same-repo PR whose headRefName == this PR's baseRefName
            # and whose head lives in the child's base repository.
            parent_candidates = head_map.get(pr.base_ref, [])
            parent_pr = next(
                (
                    p
                    for p in parent_candidates
                    if p.number != pr.number
                    and p.owner == pr.owner
                    and p.repo == pr.repo
                    and p.head_lives_in_base_repo()
                ),
                None,
            )
            if parent_pr is not None:
                parent_of[pr.number] = parent_pr.number
                children_of[parent_pr.number].append(pr.number)
            else:
                parent_of[pr.number] = None

        # Find all base PRs (no parent or parent is default branch)
        base_prs = [pr for pr in prs if parent_of.get(pr.number) is None]

        # Build stacks from each base
        stacks: list[Stack] = []
        visited: set[int] = set()

        for base_pr in base_prs:
            if base_pr.number in visited:
                continue

            # BFS/DFS from base to collect full chain
            chain: list[PRInfo] = []
            queue = [base_pr.number]

            while queue:
                pr_num = queue.pop(0)
                if pr_num in visited:
                    continue
                visited.add(pr_num)

                # Find the PR info
                pr_info = pr_map.get(pr_num)
                if pr_info is None:
                    continue
                chain.append(pr_info)

                # Add children to queue
                for child_num in children_of.get(pr_num, []):
                    if child_num not in visited:
                        queue.append(child_num)

            if len(chain) == 1:
                # Singleton — no stack needed
                continue

            # Build stack with positions
            stack_id = f"{base_pr.owner}/{base_pr.repo}/stack-{base_pr.number}"
            stack = Stack(
                stack_id=stack_id,
                repo=base_pr.repo,
                owner=base_pr.owner,
                default_branch=self.default_branch,
            )

            # Assign positions via BFS from base
            position_map: dict[int, int] = {}
            pos_queue = [(base_pr.number, 0)]

            while pos_queue:
                pr_num, pos = pos_queue.pop(0)
                if pr_num in position_map:
                    continue
                position_map[pr_num] = pos

                for child_num in children_of.get(pr_num, []):
                    if child_num not in position_map:
                        pos_queue.append((child_num, pos + 1))

            # Build members
            for pr_info in chain:
                pr_num = pr_info.number
                member = StackMember(
                    pr=pr_info,
                    stack_position=position_map.get(pr_num, 0),
                    is_base=parent_of.get(pr_num) is None,
                    children=children_of.get(pr_num, []),
                    parent=parent_of.get(pr_num),
                )
                stack.members.append(member)

            stacks.append(stack)

        # Cyclic components: every PR has a parent, so no valid bottom-up order.
        unvisited_prs = [pr for pr in prs if pr.number not in visited]
        for start_pr in unvisited_prs:
            if start_pr.number in visited:
                continue

            cycle_chain: list[PRInfo] = []
            queue = [start_pr.number]

            while queue:
                pr_num = queue.pop(0)
                if pr_num in visited:
                    continue
                visited.add(pr_num)

                pr_info = pr_map.get(pr_num)
                if pr_info is None:
                    continue
                cycle_chain.append(pr_info)

                parent_num = parent_of.get(pr_num)
                if parent_num is not None and parent_num not in visited:
                    queue.append(parent_num)
                for child_num in children_of.get(pr_num, []):
                    if child_num not in visited:
                        queue.append(child_num)

            if len(cycle_chain) < 2:
                continue
            if any(parent_of.get(pr.number) is None for pr in cycle_chain):
                continue

            cycle_base = cycle_chain[0]
            stack_id = f"{cycle_base.owner}/{cycle_base.repo}/stack-cycle-{cycle_base.number}"
            stack = Stack(
                stack_id=stack_id,
                repo=cycle_base.repo,
                owner=cycle_base.owner,
                default_branch=self.default_branch,
                is_cyclic=True,
            )

            cycle_position_map: dict[int, int] = {}
            for pos, pr_info in enumerate(sorted(cycle_chain, key=lambda p: p.number)):
                cycle_position_map[pr_info.number] = pos

            for pr_info in cycle_chain:
                pr_num = pr_info.number
                stack.members.append(
                    StackMember(
                        pr=pr_info,
                        stack_position=cycle_position_map[pr_num],
                        is_base=False,
                        children=children_of.get(pr_num, []),
                        parent=parent_of.get(pr_num),
                    )
                )

            stacks.append(stack)

        return stacks

    def detect_stacks_from_github(
        self,
        pr_data: list[dict[str, Any]],
        default_branch: str | None = None,
    ) -> list[Stack]:
        """Build stacks from GitHub API PR data.

        Parameters
        ----------
        pr_data:
            List of PR dicts from ``gh pr list --state all --json
            number,headRefName,baseRefName,state``.
        default_branch:
            Default branch name. If None, uses self.default_branch.

        Returns
        -------
        List of Stack objects with members ordered bottom-up.
        """
        if default_branch:
            self.default_branch = default_branch

        prs = []
        for data in pr_data:
            if not isinstance(data, dict):
                continue

            number = data.get("number")
            head_ref = data.get("headRefName")
            base_ref = data.get("baseRefName")
            if (
                not isinstance(number, int)
                or not isinstance(head_ref, str)
                or not isinstance(base_ref, str)
            ):
                continue

            raw_state = data.get("state", "OPEN")
            if not isinstance(raw_state, str):
                raw_state = "OPEN"
            merged_at = data.get("merged_at") or data.get("mergedAt")
            pr_owner = data.get("owner") or self.owner
            pr_repo = data.get("repo") or self.repo
            if not isinstance(pr_owner, str) or not isinstance(pr_repo, str):
                pr_owner = self.owner
                pr_repo = self.repo
            head_owner = data.get("headOwner") or data.get("head_owner")
            head_repo = data.get("headRepo") or data.get("head_repo")
            if not isinstance(head_owner, str):
                head_owner = None
            if not isinstance(head_repo, str):
                head_repo = None
            mergeable = data.get("mergeable")
            pr = PRInfo(
                number=number,
                head_ref=head_ref,
                base_ref=base_ref,
                repo=pr_repo,
                owner=pr_owner,
                state=_parse_github_pr_state(raw_state, merged_at, mergeable),
                head_owner=head_owner,
                head_repo=head_repo,
            )
            prs.append(pr)

        return self.detect_stacks(prs)

    def _resolve_repo_slug(self, repo_path: str | None) -> str:
        """Return owner/repo slug for gh API calls."""
        if repo_path and "/" in repo_path and not os.path.isdir(repo_path):
            # Validate a canonical owner/repo slug: exactly one non-empty slash pair.
            parts = repo_path.split("/")
            if len(parts) == 2 and parts[0] and parts[1]:
                return repo_path
        return f"{self.owner}/{self.repo}"

    def _fetch_all_prs_from_gh(self, repo_path: str | None) -> list[dict[str, Any]]:
        """Fetch all PRs (open, closed, merged) with pagination.

        Closed ancestors must be present so children are not misclassified as
        standalone when a parent branch is no longer open.
        """
        repo_slug = self._resolve_repo_slug(repo_path)
        cwd = repo_path if repo_path and os.path.isdir(repo_path) else None
        all_prs: list[dict[str, Any]] = []

        # Capture the source repo so PRInfo owner/repo always reflect the
        # actual queried slug, even when repo_path is a local directory.
        owner, _, repo = repo_slug.partition("/")

        page = 1

        while True:
            cmd = [
                self._gh_bin,
                "api",
                "--method",
                "GET",
                f"repos/{repo_slug}/pulls",
                "-f",
                "state=all",
                "-f",
                f"per_page={_GH_PR_PAGE_SIZE}",
                "-f",
                f"page={page}",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
                cwd=cwd,
            )
            batch: list[dict[str, Any]] = json.loads(result.stdout)
            if not batch:
                break

            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                number = raw.get("number")
                head = raw.get("head")
                base = raw.get("base")
                if (
                    not isinstance(number, int)
                    or not isinstance(head, dict)
                    or not isinstance(base, dict)
                ):
                    continue
                head_ref = head.get("ref")
                base_ref = base.get("ref")
                if not isinstance(head_ref, str) or not isinstance(base_ref, str):
                    continue
                # Preserve head repository identity (fork vs base repo).
                head_owner: str | None = owner
                head_repo: str | None = repo
                head_repo_obj = head.get("repo")
                if isinstance(head_repo_obj, dict):
                    full_name = head_repo_obj.get("full_name")
                    if isinstance(full_name, str) and "/" in full_name:
                        ho, _, hr = full_name.partition("/")
                        if ho and hr:
                            head_owner = ho
                            head_repo = hr
                    else:
                        name = head_repo_obj.get("name")
                        head_owner_obj = head_repo_obj.get("owner")
                        login = None
                        if isinstance(head_owner_obj, dict):
                            login = head_owner_obj.get("login")
                        if isinstance(login, str) and isinstance(name, str) and login and name:
                            head_owner = login
                            head_repo = name
                elif head_repo_obj is None:
                    # Deleted fork head: unknown identity — never use as parent.
                    head_owner = ""
                    head_repo = ""
                all_prs.append(
                    {
                        "number": number,
                        "headRefName": head_ref,
                        "baseRefName": base_ref,
                        "state": raw.get("state", "OPEN"),
                        "merged_at": raw.get("merged_at"),
                        # REST: true/false/null; null while GitHub computes mergeability.
                        "mergeable": raw.get("mergeable"),
                        "owner": owner,
                        "repo": repo,
                        "headOwner": head_owner,
                        "headRepo": head_repo,
                    }
                )

            if len(batch) < _GH_PR_PAGE_SIZE:
                break
            page += 1

        return all_prs

    def detect_stacks_from_gh_cli(
        self,
        repo_path: str | None = None,
    ) -> list[Stack]:
        """Detect stacks using gh CLI.

        Parameters
        ----------
        repo_path:
            Path to git repo or ``owner/repo`` slug. If None, uses current
            directory.

        Returns
        -------
        List of Stack objects.
        """
        # Get default branch
        cmd = [self._gh_bin, "repo", "view", "--json", "defaultBranchRef"]
        cwd = None
        if repo_path:
            if os.path.isdir(repo_path):
                cwd = repo_path
            else:
                cmd.append(repo_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
                cwd=cwd,
            )
            repo_data = json.loads(result.stdout)
            self.default_branch = repo_data["defaultBranchRef"]["name"]
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ):
            pass  # Use default

        pr_data = self._fetch_all_prs_from_gh(repo_path)
        return self.detect_stacks_from_github(pr_data)


def _health_for_stack(stack: Stack, stack_health: dict[str, PRState] | None) -> dict[str, PRState]:
    """Resolve stack health from explicit map or member PR states.

    Keys are ``owner/repo#n`` (:attr:`PRInfo.key`), never bare integers.
    """
    if stack_health is not None:
        return stack_health
    return {m.pr.key: m.pr.state for m in stack.members}


def _owner_allowed(pr: PRInfo, allowed_owners: set[str], allow_unlisted: bool) -> bool:
    """Return True if ``pr.owner`` is within the allowed owner set.

    When ``allow_unlisted`` is False and ``allowed_owners`` is empty, nothing
    is allowed (safe default).
    """
    if allow_unlisted:
        return True
    if not allowed_owners:
        return False
    return pr.owner in allowed_owners


def order_prs_bottom_up(
    stacks: list[Stack],
    standalone: list[PRInfo],
    stack_health: dict[str, PRState] | None = None,
    allowed_owners: set[str] | frozenset[str] | None = None,
    allow_unlisted: bool = False,
) -> list[PRInfo]:
    """Order PRs for processing: bottom-up within stacks, then standalone.

    Parameters
    ----------
    stacks:
        Detected stacks with members.
    standalone:
        Non-stacked PRs (can process in any order).
    stack_health:
        Optional map of ``owner/repo#n`` → :class:`PRState` for blocking
        decisions. When omitted, each member's ``PRInfo.state`` is used.
    allowed_owners:
        Set of allowed owner names. When ``None``, owners are loaded from
        ``WH_ALLOWED_OWNERS`` (comma-separated) or fall back to
        :data:`DEFAULT_ALLOWED_OWNERS` (empty). An empty allowlist with
        ``allow_unlisted=False`` schedules nothing.
    allow_unlisted:
        If True, skip owner filtering and allow any owner.

    Returns
    -------
    Ordered list of processable PRs: stack bottoms first, then children, then
    standalone. Blocked stack members, cyclic stacks, and non-open PRs are
    omitted. PRs whose owner is not in the allowlist are omitted unless
    ``allow_unlisted`` is set.
    """
    allowed = resolve_allowed_owners(allowed_owners)
    # Safe default: empty allowlist and no override → schedule nothing.
    if not allow_unlisted and not allowed:
        return []

    ordered: list[PRInfo] = []

    for stack in sorted(stacks, key=lambda s: s.stack_id):
        health = _health_for_stack(stack, stack_health)
        ordered.extend(
            pr
            for pr in stack.get_processable(health)
            if _owner_allowed(pr, allowed, allow_unlisted)
        )

    ordered.extend(
        pr
        for pr in standalone
        if _owner_allowed(pr, allowed, allow_unlisted) and pr.state == PRState.OPEN
    )

    return ordered


def find_standalone_prs(
    all_prs: list[PRInfo],
    stacks: list[Stack],
    allowed_owners: set[str] | frozenset[str] | None = None,
    allow_unlisted: bool = False,
) -> list[PRInfo]:
    """Find PRs not part of any stack.

    Parameters
    ----------
    all_prs:
        All open PRs.
    stacks:
        Detected stacks.
    allowed_owners:
        Set of allowed owner names. When ``None``, owners are loaded from
        ``WH_ALLOWED_OWNERS`` or :data:`DEFAULT_ALLOWED_OWNERS` (empty).
        Empty allowlist with ``allow_unlisted=False`` returns nothing.
    allow_unlisted:
        If True, skip owner filtering.

    Returns
    -------
    PRs not in any stack (and allowed by the owner filter).
    """
    allowed = resolve_allowed_owners(allowed_owners)
    if not allow_unlisted and not allowed:
        return []

    stacked_keys = set()
    for stack in stacks:
        for member in stack.members:
            stacked_keys.add(member.pr.key)

    return [
        pr
        for pr in all_prs
        if pr.key not in stacked_keys
        and _owner_allowed(pr, allowed, allow_unlisted)
        and pr.state == PRState.OPEN
    ]
