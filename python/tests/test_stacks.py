"""Tests for stack-aware PR ordering."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.stacks import (
    DEFAULT_ALLOWED_OWNERS,
    PRInfo,
    PRState,
    Stack,
    StackDetector,
    StackMember,
    find_standalone_prs,
    load_allowed_owners_from_env,
    order_prs_bottom_up,
    resolve_allowed_owners,
)

# Generic fixture owners — never product org names.
TEST_OWNER = "acme"
TEST_REPO = "worktrees-hives"
OTHER_OWNER = "example-org"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_pr(
    number: int,
    head: str,
    base: str,
    owner: str = TEST_OWNER,
    repo: str = TEST_REPO,
    state: PRState = PRState.OPEN,
) -> PRInfo:
    """Helper to create PRInfo."""
    return PRInfo(
        number=number,
        head_ref=head,
        base_ref=base,
        repo=repo,
        owner=owner,
        state=state,
    )


def health_map(*entries: tuple[PRInfo, PRState] | tuple[int, PRState]) -> dict[str, PRState]:
    """Build stack_health keyed by ``owner/repo#n``.

    Accepts either ``(PRInfo, PRState)`` or ``(number, PRState)`` using
    :data:`TEST_OWNER` / :data:`TEST_REPO` for bare numbers.
    """
    out: dict[str, PRState] = {}
    for entry in entries:
        key_or_pr, state = entry
        if isinstance(key_or_pr, PRInfo):
            out[key_or_pr.key] = state
        else:
            out[f"{TEST_OWNER}/{TEST_REPO}#{key_or_pr}"] = state
    return out


# Tests that schedule PRs must always pass allowed_owners or allow_unlisted.
ALLOWED = {TEST_OWNER}


@pytest.fixture
def detector() -> StackDetector:
    return StackDetector(owner=TEST_OWNER, repo=TEST_REPO, default_branch="main")


@pytest.fixture
def sample_stack_prs() -> list[PRInfo]:
    """A 3-deep stack: #1 (base) -> #2 -> #3."""
    return [
        make_pr(1, "feat/base", "main"),
        make_pr(2, "feat/child", "feat/base"),
        make_pr(3, "feat/grandchild", "feat/child"),
    ]


@pytest.fixture
def two_independent_stacks() -> list[PRInfo]:
    """Two independent stacks."""
    return [
        # Stack A: #10 -> #11
        make_pr(10, "feat/a-base", "main"),
        make_pr(11, "feat/a-child", "feat/a-base"),
        # Stack B: #20 -> #21 -> #22
        make_pr(20, "feat/b-base", "main"),
        make_pr(21, "feat/b-mid", "feat/b-base"),
        make_pr(22, "feat/b-tip", "feat/b-mid"),
    ]


@pytest.fixture
def mixed_prs() -> list[PRInfo]:
    """Mix of stacked and standalone PRs."""
    return [
        # Standalone
        make_pr(5, "fix/standalone", "main"),
        # Stack: #10 -> #11
        make_pr(10, "feat/base", "main"),
        make_pr(11, "feat/child", "feat/base"),
        # Another standalone
        make_pr(15, "docs/update", "main"),
    ]


# ---------------------------------------------------------------------------
# PRInfo
# ---------------------------------------------------------------------------


class TestPRInfo:
    def test_key_format(self):
        pr = make_pr(42, "feat/x", "main", owner=TEST_OWNER, repo="test")
        assert pr.key == f"{TEST_OWNER}/test#42"

    def test_frozen(self):
        pr = make_pr(1, "a", "b")
        with pytest.raises(AttributeError):
            pr.number = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# StackMember
# ---------------------------------------------------------------------------


class TestStackMember:
    def test_base_member_can_process(self):
        pr = make_pr(1, "feat/base", "main")
        member = StackMember(pr=pr, stack_position=0, is_base=True)
        assert member._can_process({}) is True

    def test_child_with_healthy_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member._can_process(health_map((1, PRState.OPEN))) is True

    def test_child_with_conflicting_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member._can_process(health_map((1, PRState.CONFLICTING))) is False

    def test_child_with_merged_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        # Merged parent is "done" — child can process
        assert member._can_process(health_map((1, PRState.MERGED))) is True

    def test_child_with_closed_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        # Closed parent means chain is dead — child cannot process
        assert member._can_process(health_map((1, PRState.CLOSED))) is False

    def test_child_with_unknown_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member._can_process(health_map((1, PRState.UNKNOWN))) is False

    def test_is_blocked_base_not_blocked(self):
        pr = make_pr(1, "feat/base", "main")
        member = StackMember(pr=pr, stack_position=0, is_base=True)
        assert member._is_blocked() is False

    def test_is_blocked_child_without_health(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        # Without health info, assumes blocked
        assert member._is_blocked() is True

    def test_is_blocked_child_with_healthy_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member._is_blocked(health_map((1, PRState.OPEN))) is False

    def test_is_blocked_child_with_unhealthy_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member._is_blocked(health_map((1, PRState.CONFLICTING))) is True

    def test_is_blocked_child_with_closed_parent(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member._is_blocked(health_map((1, PRState.CLOSED))) is True

    def test_parent_health_key_format(self):
        pr = make_pr(2, "feat/child", "feat/base")
        member = StackMember(pr=pr, stack_position=1, is_base=False, parent=1)
        assert member.parent_health_key() == f"{TEST_OWNER}/{TEST_REPO}#1"


# ---------------------------------------------------------------------------
# StackDetector.detect_stacks
# ---------------------------------------------------------------------------


class TestDetectStacks:
    def test_empty_input(self, detector: StackDetector):
        assert detector.detect_stacks([]) == []

    def test_single_pr_no_stack(self, detector: StackDetector):
        prs = [make_pr(1, "feat/x", "main")]
        stacks = detector.detect_stacks(prs)
        assert stacks == []  # singleton = no stack

    def test_simple_two_pr_stack(self, detector: StackDetector):
        prs = [
            make_pr(1, "feat/base", "main"),
            make_pr(2, "feat/child", "feat/base"),
        ]
        stacks = detector.detect_stacks(prs)
        assert len(stacks) == 1
        stack = stacks[0]
        assert len(stack.members) == 2
        assert stack.stack_id == f"{TEST_OWNER}/{TEST_REPO}/stack-1"

        # Verify bottom-up ordering
        positions = {m.pr.number: m.stack_position for m in stack.members}
        assert positions[1] == 0  # base
        assert positions[2] == 1  # child

    def test_three_deep_stack(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        assert len(stacks) == 1
        stack = stacks[0]
        assert len(stack.members) == 3

        positions = {m.pr.number: m.stack_position for m in stack.members}
        assert positions[1] == 0
        assert positions[2] == 1
        assert positions[3] == 2

    def test_two_independent_stacks(self, detector: StackDetector, two_independent_stacks):
        stacks = detector.detect_stacks(two_independent_stacks)
        assert len(stacks) == 2

        stack_ids = {s.stack_id for s in stacks}
        assert f"{TEST_OWNER}/{TEST_REPO}/stack-10" in stack_ids
        assert f"{TEST_OWNER}/{TEST_REPO}/stack-20" in stack_ids

    def test_mixed_stacked_and_standalone(self, detector: StackDetector, mixed_prs):
        stacks = detector.detect_stacks(mixed_prs)
        # Only #10->#11 forms a stack; #5 and #15 are standalone
        assert len(stacks) == 1
        assert len(stacks[0].members) == 2

    def test_diamond_not_possible_in_github(self, detector: StackDetector):
        """GitHub PRs have one base, so diamond shapes don't occur."""
        prs = [
            make_pr(1, "feat/a", "main"),
            make_pr(2, "feat/b", "feat/a"),
            make_pr(3, "feat/c", "feat/a"),
        ]
        stacks = detector.detect_stacks(prs)
        # #1 is base, #2 and #3 are children — forms one stack with branching
        assert len(stacks) == 1
        stack = stacks[0]
        assert len(stack.members) == 3

        # #1 should have two children
        base_member = stack.get_member(1)
        assert base_member is not None
        assert len(base_member.children) == 2

    def test_external_base_not_in_stack(self, detector: StackDetector):
        """PR targeting a non-open branch (external base) is standalone."""
        prs = [
            make_pr(1, "feat/x", "release/v2"),  # base not in open PRs
        ]
        stacks = detector.detect_stacks(prs)
        assert stacks == []  # standalone

    def test_duplicate_head_ref_preserves_all_prs(self, detector: StackDetector):
        """Multiple PRs sharing the same head_ref must not be silently dropped."""
        prs = [
            make_pr(1, "feat/shared", "main"),
            make_pr(2, "feat/shared", "main"),  # same head_ref as #1
            make_pr(3, "feat/child", "feat/shared"),  # bases on shared branch
        ]
        stacks = detector.detect_stacks(prs)
        # Both #1 and #2 should exist; #3 should parent to one of them
        all_members = [m for s in stacks for m in s.members] if stacks else []
        all_prs_in_stacks = [m.pr.number for m in all_members]
        # #1 and #2 are singletons (same head_ref, base="main"), so no stack.
        # But #3 parents to the first match of "feat/shared" -> #1.
        # #1 -> #3 forms a stack.
        assert len(stacks) == 1
        assert 3 in all_prs_in_stacks

    def test_cycle_detection(self, detector: StackDetector):
        """Circular refs form a blocked cyclic stack, not standalone PRs."""
        prs = [
            make_pr(1, "feat/a", "feat/b"),
            make_pr(2, "feat/b", "feat/a"),
        ]
        stacks = detector.detect_stacks(prs)
        assert len(stacks) == 1
        assert stacks[0].is_cyclic is True
        h = health_map((1, PRState.OPEN), (2, PRState.OPEN))
        assert stacks[0].get_processable(h) == []
        standalone = find_standalone_prs(prs, stacks, allowed_owners=ALLOWED)
        assert standalone == []


# ---------------------------------------------------------------------------
# StackDetector.detect_stacks_from_github
# ---------------------------------------------------------------------------


class TestDetectStacksFromGithub:
    def test_parses_github_api_format(self, detector: StackDetector):
        pr_data = [
            {"number": 1, "headRefName": "feat/base", "baseRefName": "main"},
            {"number": 2, "headRefName": "feat/child", "baseRefName": "feat/base"},
        ]
        stacks = detector.detect_stacks_from_github(pr_data, default_branch="main")
        assert len(stacks) == 1
        assert len(stacks[0].members) == 2

    def test_parses_merged_state_from_github(self, detector: StackDetector):
        pr_data = [
            {
                "number": 1,
                "headRefName": "feat/base",
                "baseRefName": "main",
                "state": "CLOSED",
                "merged_at": "2026-07-01T12:00:00Z",
            },
            {
                "number": 2,
                "headRefName": "feat/child",
                "baseRefName": "feat/base",
                "state": "OPEN",
            },
        ]
        stacks = detector.detect_stacks_from_github(pr_data)
        stack = stacks[0]
        merged_parent = stack.get_member(1)
        assert merged_parent is not None
        assert merged_parent.pr.state == PRState.MERGED
        health = health_map((1, PRState.MERGED), (2, PRState.OPEN))
        assert stack.get_processable(health) == [stack.get_member(2).pr]

    def test_closed_non_merged_parent_blocks_open_child(self, detector: StackDetector):
        """A closed (not merged) parent and open child stay in one stack;
        the child is blocked by the closed ancestor."""
        pr_data = [
            {
                "number": 1,
                "headRefName": "feat/base",
                "baseRefName": "main",
                "state": "CLOSED",
                "merged_at": None,
            },
            {
                "number": 2,
                "headRefName": "feat/child",
                "baseRefName": "feat/base",
                "state": "OPEN",
            },
        ]
        stacks = detector.detect_stacks_from_github(pr_data)
        assert len(stacks) == 1
        stack = stacks[0]
        assert len(stack.members) == 2
        parent = stack.get_member(1)
        child = stack.get_member(2)
        assert parent is not None and parent.pr.state == PRState.CLOSED
        assert child is not None and child.parent == 1
        assert stack.get_processable({}) == []

    def test_detect_stacks_from_github_preserves_source_repo(self, detector: StackDetector):
        """PRInfo owner/repo must come from the data, not the detector config."""
        pr_data = [
            {
                "number": 1,
                "headRefName": "feat/base",
                "baseRefName": "main",
                "owner": OTHER_OWNER,
                "repo": "other-repo",
            },
            {
                "number": 2,
                "headRefName": "feat/child",
                "baseRefName": "feat/base",
                "owner": OTHER_OWNER,
                "repo": "other-repo",
            },
        ]
        stacks = detector.detect_stacks_from_github(pr_data)
        stack = stacks[0]
        assert stack.owner == OTHER_OWNER
        assert stack.repo == "other-repo"
        assert stack.get_member(1).pr.owner == OTHER_OWNER
        assert stack.get_member(1).pr.repo == "other-repo"

    def test_custom_default_branch(self, detector: StackDetector):
        pr_data = [
            {"number": 1, "headRefName": "feat/x", "baseRefName": "develop"},
            {"number": 2, "headRefName": "feat/y", "baseRefName": "feat/x"},
        ]
        stacks = detector.detect_stacks_from_github(pr_data, default_branch="develop")
        assert len(stacks) == 1


# ---------------------------------------------------------------------------
# StackDetector.detect_stacks_from_gh_cli
# ---------------------------------------------------------------------------


class TestDetectStacksFromGhCli:
    def _repo_view_response(self):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({"defaultBranchRef": {"name": "main"}}),
            stderr="",
        )

    def _api_response(self, prs):
        return MagicMock(
            returncode=0,
            stdout=json.dumps(prs),
            stderr="",
        )

    def _make_gh_api_data(
        self,
        number,
        head_ref,
        base_ref,
        state="open",
        merged_at=None,
        owner=OTHER_OWNER,
        repo="other-repo",
    ):
        return {
            "number": number,
            "head": {"ref": head_ref},
            "base": {"ref": base_ref},
            "state": state,
            "merged_at": merged_at,
            "owner": owner,
            "repo": repo,
        }

    @patch("worktrees_hives.stacks.subprocess.run")
    def test_detect_stacks_from_gh_cli(self, mock_run):
        detector = StackDetector(owner=TEST_OWNER, repo=TEST_REPO)
        prs = [
            self._make_gh_api_data(1, "feat/base", "main"),
            self._make_gh_api_data(2, "feat/child", "feat/base"),
        ]
        mock_run.side_effect = [
            self._repo_view_response(),
            self._api_response(prs),
        ]

        stacks = detector.detect_stacks_from_gh_cli(f"{OTHER_OWNER}/other-repo")

        assert len(stacks) == 1
        assert stacks[0].owner == OTHER_OWNER
        assert stacks[0].repo == "other-repo"
        assert stacks[0].get_member(1).pr.owner == OTHER_OWNER

    @patch("worktrees_hives.stacks.subprocess.run")
    def test_detect_stacks_from_gh_cli_uses_default_branch_on_timeout(self, mock_run):
        detector = StackDetector(owner=TEST_OWNER, repo=TEST_REPO)
        prs = [
            self._make_gh_api_data(1, "feat/base", "main"),
            self._make_gh_api_data(2, "feat/child", "feat/base"),
        ]
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="gh", timeout=30.0),
            self._api_response(prs),
        ]

        stacks = detector.detect_stacks_from_gh_cli(f"{OTHER_OWNER}/other-repo")

        assert len(stacks) == 1
        assert detector.default_branch == "main"


class TestFetchAllPrsFromGh:
    @patch("worktrees_hives.stacks.subprocess.run")
    def test_fetch_all_prs_from_gh_pagination(self, mock_run):
        from worktrees_hives.stacks import _GH_PR_PAGE_SIZE

        detector = StackDetector(owner=TEST_OWNER, repo=TEST_REPO)
        page1 = [
            {
                "number": i,
                "head": {"ref": f"feat/{i}"},
                "base": {"ref": "main"},
                "state": "open",
                "merged_at": None,
            }
            for i in range(1, _GH_PR_PAGE_SIZE + 1)
        ]
        page2 = [
            {
                "number": _GH_PR_PAGE_SIZE + 1,
                "head": {"ref": "feat/tip"},
                "base": {"ref": "main"},
                "state": "open",
                "merged_at": None,
            }
        ]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            page_param = None
            for i, token in enumerate(cmd):
                if token == "-f" and i + 1 < len(cmd) and cmd[i + 1].startswith("page="):
                    page_param = int(cmd[i + 1].split("=", 1)[1])
            if page_param == 1:
                return MagicMock(returncode=0, stdout=json.dumps(page1), stderr="")
            return MagicMock(returncode=0, stdout=json.dumps(page2), stderr="")

        mock_run.side_effect = side_effect

        all_prs = detector._fetch_all_prs_from_gh(f"{OTHER_OWNER}/other-repo")
        numbers = {pr["number"] for pr in all_prs}
        assert len(numbers) == _GH_PR_PAGE_SIZE + 1


# ---------------------------------------------------------------------------
# Stack methods
# ---------------------------------------------------------------------------


class TestStack:
    def test_ordered_prs(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        ordered = stack.ordered_prs
        assert [pr.number for pr in ordered] == [1, 2, 3]

    def test_get_member(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        member = stack.get_member(2)
        assert member is not None
        assert member.pr.number == 2
        assert stack.get_member(999) is None

    def test_get_processable_all_healthy(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        health = health_map((1, PRState.OPEN), (2, PRState.OPEN), (3, PRState.OPEN))
        processable = stack.get_processable(health)
        assert [pr.number for pr in processable] == [1, 2, 3]

    def test_get_processable_parent_blocked(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        # Parent (#1) is conflicting — children blocked
        health = health_map((1, PRState.CONFLICTING), (2, PRState.OPEN), (3, PRState.OPEN))
        processable = stack.get_processable(health)
        # Only base can process (it has no parent)
        assert [pr.number for pr in processable] == [1]

    def test_get_processable_mid_blocked(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        # #2 is conflicting — #3 blocked
        health = health_map((1, PRState.OPEN), (2, PRState.CONFLICTING), (3, PRState.OPEN))
        processable = stack.get_processable(health)
        assert [pr.number for pr in processable] == [1, 2]

    def test_get_processable_parent_closed(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        # #1 is closed (not merged) — chain is dead
        health = health_map((1, PRState.CLOSED), (2, PRState.OPEN), (3, PRState.OPEN))
        processable = stack.get_processable(health)
        # Only base can process; #2 and #3 blocked by closed parent
        assert [pr.number for pr in processable] == [1]

    def test_get_processable_closed_base_not_scheduled(self):
        # A closed base with an open child should not be returned, even if it
        # has no parent.
        prs = [
            make_pr(1, "feat/base", "main", state=PRState.CLOSED),
            make_pr(2, "feat/child", "feat/base"),
        ]
        stack = Stack(
            stack_id=f"{TEST_OWNER}/{TEST_REPO}/stack-1",
            repo=TEST_REPO,
            owner=TEST_OWNER,
            members=[
                StackMember(pr=prs[0], stack_position=0, is_base=True),
                StackMember(pr=prs[1], stack_position=1, is_base=False, parent=1),
            ],
        )
        health = health_map((1, PRState.CLOSED), (2, PRState.OPEN))
        assert stack.get_processable(health) == []

    def test_get_blocked(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        health = health_map((1, PRState.CONFLICTING), (2, PRState.OPEN), (3, PRState.OPEN))
        blocked = stack.get_blocked(health)
        # #2 and #3 are blocked (parent #1 is conflicting)
        assert len(blocked) == 2

    def test_bottom_property(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        stack = stacks[0]
        bottom = stack.bottom
        assert bottom is not None
        assert bottom.pr.number == 1

    def test_is_empty(self, detector: StackDetector):
        stack = Stack(stack_id="test", repo="r", owner="o", default_branch="main")
        assert stack.is_empty is True


# ---------------------------------------------------------------------------
# order_prs_bottom_up
# ---------------------------------------------------------------------------


class TestOrderPrsBottomUp:
    def test_order_with_stacks(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        standalone = [make_pr(99, "fix/other", "main")]
        ordered = order_prs_bottom_up(stacks, standalone, allowed_owners=ALLOWED)
        assert [pr.number for pr in ordered] == [1, 2, 3, 99]

    def test_order_standalone_only(self):
        prs = [make_pr(5, "a", "main"), make_pr(3, "b", "main")]
        ordered = order_prs_bottom_up([], prs, allowed_owners=ALLOWED)
        assert [pr.number for pr in ordered] == [5, 3]

    def test_order_multiple_stacks(self, detector: StackDetector, two_independent_stacks):
        stacks = detector.detect_stacks(two_independent_stacks)
        ordered = order_prs_bottom_up(stacks, [], allowed_owners=ALLOWED)
        numbers = [pr.number for pr in ordered]
        # Each stack is ordered bottom-up; stacks sorted by stack_id
        assert numbers == [10, 11, 20, 21, 22]

    def test_order_filters_blocked_stack_members(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        health = health_map((1, PRState.CONFLICTING), (2, PRState.OPEN), (3, PRState.OPEN))
        ordered = order_prs_bottom_up(stacks, [], stack_health=health, allowed_owners=ALLOWED)
        assert [pr.number for pr in ordered] == [1]


# ---------------------------------------------------------------------------
# find_standalone_prs
# ---------------------------------------------------------------------------


class TestFindStandalonePrs:
    def test_all_standalone(self, detector: StackDetector):
        prs = [make_pr(1, "a", "main"), make_pr(2, "b", "main")]
        stacks = detector.detect_stacks(prs)
        standalone = find_standalone_prs(prs, stacks, allowed_owners=ALLOWED)
        assert len(standalone) == 2

    def test_none_standalone(self, detector: StackDetector, sample_stack_prs):
        stacks = detector.detect_stacks(sample_stack_prs)
        standalone = find_standalone_prs(sample_stack_prs, stacks, allowed_owners=ALLOWED)
        assert len(standalone) == 0

    def test_mixed(self, detector: StackDetector, mixed_prs):
        stacks = detector.detect_stacks(mixed_prs)
        standalone = find_standalone_prs(mixed_prs, stacks, allowed_owners=ALLOWED)
        assert len(standalone) == 2
        numbers = {pr.number for pr in standalone}
        assert numbers == {5, 15}

    def test_multi_repo_pr_number_collision(self, detector: StackDetector):
        """PR numbers collide across repos — dedupe by full PR key."""
        pr_a = make_pr(5, "feat/a", "main", owner=TEST_OWNER, repo="repo-a")
        pr_b = make_pr(5, "feat/b", "main", owner=TEST_OWNER, repo="repo-b")
        stack = Stack(
            stack_id=f"{TEST_OWNER}/repo-a/stack-5",
            repo="repo-a",
            owner=TEST_OWNER,
            members=[
                StackMember(pr=pr_a, stack_position=0, is_base=True),
            ],
        )
        standalone = find_standalone_prs([pr_a, pr_b], [stack], allowed_owners=ALLOWED)
        assert [pr.key for pr in standalone] == [f"{TEST_OWNER}/repo-b#5"]


class TestOrderPrsOwnerAllowlist:
    """Tests for the owner allowlist applied at scheduling time."""

    def test_default_allowlist_is_empty(self):
        assert not DEFAULT_ALLOWED_OWNERS

    def test_empty_default_denies_all(self, detector: StackDetector):
        """Empty default allowlist with no override schedules nothing."""
        prs = [
            make_pr(1, "feat/base", "main"),
            make_pr(2, "fix/child", "feat/base"),
        ]
        stacks = detector.detect_stacks(prs)
        standalone = [make_pr(99, "fix/other", "main")]
        # No allowed_owners, no allow_unlisted → deny all
        ordered = order_prs_bottom_up(stacks, standalone)
        assert ordered == []

    def test_order_rejects_unlisted_owner(self, detector: StackDetector):
        stacks = detector.detect_stacks([make_pr(1, "feat/base", "main", owner=OTHER_OWNER)])
        standalone = [make_pr(2, "fix/x", "main", owner=OTHER_OWNER)]
        ordered = order_prs_bottom_up(stacks, standalone, allowed_owners={TEST_OWNER})
        assert ordered == []

    def test_order_allows_unlisted_with_override(self, detector: StackDetector):
        prs = [
            make_pr(1, "feat/base", "main", owner=OTHER_OWNER),
            make_pr(2, "fix/child", "feat/base", owner=OTHER_OWNER),
        ]
        stacks = detector.detect_stacks(prs)
        standalone: list[PRInfo] = []
        ordered = order_prs_bottom_up(stacks, standalone, allow_unlisted=True)
        assert [pr.number for pr in ordered] == [1, 2]

    def test_order_allows_custom_owner_set(self, detector: StackDetector):
        prs = [
            make_pr(1, "feat/base", "main", owner="custom"),
            make_pr(2, "fix/child", "feat/base", owner="custom"),
        ]
        stacks = detector.detect_stacks(prs)
        standalone: list[PRInfo] = []
        ordered = order_prs_bottom_up(stacks, standalone, allowed_owners={"custom"})
        assert [pr.number for pr in ordered] == [1, 2]

    def test_env_wh_allowed_owners(self, detector: StackDetector, monkeypatch):
        monkeypatch.setenv("WH_ALLOWED_OWNERS", f"{TEST_OWNER},{OTHER_OWNER}")
        prs = [
            make_pr(1, "feat/base", "main", owner=TEST_OWNER),
            make_pr(2, "fix/child", "feat/base", owner=TEST_OWNER),
        ]
        stacks = detector.detect_stacks(prs)
        # allowed_owners=None → load from env
        ordered = order_prs_bottom_up(stacks, [])
        assert [pr.number for pr in ordered] == [1, 2]

    def test_explicit_empty_set_denies(self, detector: StackDetector, monkeypatch):
        # Explicit empty set must not fall back to env.
        monkeypatch.setenv("WH_ALLOWED_OWNERS", TEST_OWNER)
        prs = [make_pr(1, "feat/x", "main")]
        ordered = order_prs_bottom_up([], prs, allowed_owners=set())
        assert ordered == []


class TestFindStandalonePrsOwnerAllowlist:
    """Tests for owner filtering in find_standalone_prs."""

    def test_empty_default_denies(self):
        prs = [make_pr(1, "a", "main")]
        assert find_standalone_prs(prs, []) == []

    def test_find_standalone_filters_by_owner(self, detector: StackDetector):
        prs = [make_pr(1, "a", "main", owner=OTHER_OWNER)]
        standalone = find_standalone_prs(prs, [], allowed_owners={TEST_OWNER})
        assert standalone == []

    def test_find_standalone_allows_unlisted_with_override(self):
        pr = make_pr(1, "a", "main", owner=OTHER_OWNER)
        standalone = find_standalone_prs([pr], [], allow_unlisted=True)
        assert standalone == [pr]


class TestResolveAllowedOwners:
    def test_load_from_env(self):
        owners = load_allowed_owners_from_env(
            {"WH_ALLOWED_OWNERS": f" {TEST_OWNER}, {OTHER_OWNER} , "}
        )
        assert owners == frozenset({TEST_OWNER, OTHER_OWNER})

    def test_load_empty_env(self):
        assert load_allowed_owners_from_env({}) == frozenset()

    def test_resolve_precedence_explicit(self, monkeypatch):
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "from-env")
        assert resolve_allowed_owners({"explicit"}) == {"explicit"}

    def test_resolve_from_env(self, monkeypatch):
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "from-env")
        assert resolve_allowed_owners(None) == {"from-env"}

    def test_resolve_default_empty(self, monkeypatch):
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        assert resolve_allowed_owners(None) == set()


class TestMultiRepoAndDefaultBranchParents:
    def test_multi_repo_numbers_do_not_collide(self):
        """Same PR numbers in different repos must not form one stack."""
        prs = [
            PRInfo(number=1, head_ref="feat-a", base_ref="main", repo="a", owner=TEST_OWNER),
            PRInfo(
                number=2,
                head_ref="feat-a-2",
                base_ref="feat-a",
                repo="a",
                owner=TEST_OWNER,
            ),
            PRInfo(number=1, head_ref="feat-b", base_ref="main", repo="b", owner=TEST_OWNER),
            PRInfo(
                number=2,
                head_ref="feat-b-2",
                base_ref="feat-b",
                repo="b",
                owner=TEST_OWNER,
            ),
        ]
        detector = StackDetector(owner=TEST_OWNER, repo="a", default_branch="main")
        stacks = detector.detect_stacks(prs)
        assert len(stacks) == 2
        repos = {s.repo for s in stacks}
        assert repos == {"a", "b"}
        for s in stacks:
            assert all(m.pr.repo == s.repo for m in s.members)
            assert len(s.members) == 2

    def test_default_branch_head_not_used_as_parent(self):
        """A fork PR with head == default branch must not parent all main-base PRs."""
        prs = [
            PRInfo(
                number=10,
                head_ref="main",
                base_ref="main",
                repo="repo",
                owner=TEST_OWNER,
            ),
            PRInfo(
                number=11,
                head_ref="feature-x",
                base_ref="main",
                repo="repo",
                owner=TEST_OWNER,
            ),
            PRInfo(
                number=12,
                head_ref="feature-y",
                base_ref="main",
                repo="repo",
                owner=TEST_OWNER,
            ),
        ]
        detector = StackDetector(owner=TEST_OWNER, repo="repo", default_branch="main")
        stacks = detector.detect_stacks(prs)
        # No stack: 11 and 12 target main and must remain standalone
        assert stacks == []

    def test_fork_head_ref_not_used_as_parent(self):
        """Fork PR head named ``feature`` must not parent a base-repo PR targeting feature."""
        prs = [
            PRInfo(
                number=20,
                head_ref="feature",
                base_ref="main",
                repo="repo",
                owner=TEST_OWNER,
                head_owner="contributor",
                head_repo="repo",
            ),
            PRInfo(
                number=21,
                head_ref="feature-child",
                base_ref="feature",
                repo="repo",
                owner=TEST_OWNER,
            ),
        ]
        detector = StackDetector(owner=TEST_OWNER, repo="repo", default_branch="main")
        stacks = detector.detect_stacks(prs)
        # No stack: 21's base_ref matches fork head name, not a base-repo head.
        assert stacks == []

    def test_same_repo_stack_still_detected_with_head_identity(self):
        """Explicit same-repo head identity still forms a stack."""
        prs = [
            PRInfo(
                number=30,
                head_ref="feature",
                base_ref="main",
                repo="repo",
                owner=TEST_OWNER,
                head_owner=TEST_OWNER,
                head_repo="repo",
            ),
            PRInfo(
                number=31,
                head_ref="feature-child",
                base_ref="feature",
                repo="repo",
                owner=TEST_OWNER,
                head_owner=TEST_OWNER,
                head_repo="repo",
            ),
        ]
        detector = StackDetector(owner=TEST_OWNER, repo="repo", default_branch="main")
        stacks = detector.detect_stacks(prs)
        assert len(stacks) == 1
        assert {m.pr.number for m in stacks[0].members} == {30, 31}
