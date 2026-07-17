"""Claim an issue or PR by creating an isolated branch and git worktree.

Each agent job gets its own worktree + branch so parallel hive workers never
clobber each other.  Python owns orchestration policy (naming, path derivation,
one-claim-per-job, identity binding).  Git mutations currently run as
**sandboxed subprocesses** in ``repo_root`` with path/ref validation.

When a real ``wh worktree create|remove`` CLI is available (R2 / GH #25), prefer
routing through :class:`~worktrees_hives.bridge.WhClient`. Until then, this
module does **not** claim hard Rust enforcement via an unused client.

Worktree path layout::

    {worktree_base}/{owner}/{repo}/{job_id}

Branch naming conventions:

- Issues: ``hive/gh-{number}`` (e.g. ``hive/gh-42``)
- PRs:    checkout the existing PR head branch (no rename)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from worktrees_hives.errors import WhError

if TYPE_CHECKING:
    from worktrees_hives.bridge import WhClient

# Environment override for the worktree base directory.
_WORKTREE_BASE_ENV = "WH_WORKTREE_BASE"

# Slug pattern for branch-safe identifiers.
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Env: comma-separated owners (empty = no restriction). Never hardcode orgs.
_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"
# When set to 1/true, allow claims if origin URL cannot be parsed as owner/repo.
_ALLOW_UNVERIFIED_REMOTE_ENV = "WH_ALLOW_UNVERIFIED_REMOTE"


def _sanitize_diagnostic(text: str, max_len: int = 500) -> str:
    """Return a bounded, redacted version of subprocess output.

    Strips ANSI escapes, truncates to ``max_len``, and redacts common
    secret patterns so exception messages cannot leak credentials.
    Kept local to claim so this PR does not fork ``errors.py``.
    """
    if not isinstance(text, str):
        text = str(text)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(
        r"(https?://)[^:\s]+:[^@\s]+@",
        r"\1<redacted>@",
        text,
        flags=re.IGNORECASE,
    )
    for pattern, replacement in (
        (r"gh[po]_[A-Za-z0-9_]+", "<token>"),
        (r"github_pat_[A-Za-z0-9_]+", "<token>"),
        (r"\b(?:api[_-]?key|token|password|secret|credential)[=:]\s*\S+", "<secret>"),
        (r"Authorization:\s*(?:Bearer|token|Basic)\s+\S+", "Authorization: <redacted>"),
    ):
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text.strip()


def _redact_repo_url(repo: str) -> str:
    """Redact credentials from git remote URLs before including in exceptions."""
    if not repo or not isinstance(repo, str):
        return str(repo)
    return _sanitize_diagnostic(repo)


def _default_worktree_base() -> str:
    """Platform-aware default under WH_WORKTREE_BASE / XDG / OS user-data dirs."""
    if override := os.environ.get(_WORKTREE_BASE_ENV):
        return override
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local:
            return os.path.join(local, "worktrees-hives", "worktrees")
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
            "worktrees-hives",
            "worktrees",
        )
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "worktrees-hives", "worktrees")
    return os.path.join(
        os.path.expanduser("~"),
        ".local",
        "share",
        "worktrees-hives",
        "worktrees",
    )


def _load_allowed_owners_from_env() -> frozenset[str]:
    """Load allowed owners from env var.

    When ``WH_ALLOWED_OWNERS`` is unset, empty, or ``*``, any owner is accepted.
    Comma-separated values restrict to that explicit allowlist. Operators configure
    restriction via env or :class:`ClaimManager` ``allowed_owners=``.
    """
    if _ALLOWED_OWNERS_ENV not in os.environ:
        return frozenset()
    raw = os.environ[_ALLOWED_OWNERS_ENV].strip()
    if not raw or raw == "*":
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class ClaimError(WhError):
    """Raised when a claim operation fails at the orchestration layer."""


class ClaimExistsError(ClaimError):
    """Raised when a worktree already exists for the given job id."""


class IsolationError(ClaimError):
    """Raised when branch verification fails before edits."""


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Successful claim metadata returned to the caller (orchestrator / agent)."""

    owner: str
    repo: str
    job_id: str
    branch: str
    worktree_path: str
    issue_number: int | None = None
    pr_number: int | None = None
    # True when this claim created the local branch (issue claims).
    # False for PR claims that check out an existing head branch.
    owns_branch: bool = True


@dataclass
class ClaimManager:
    """Manages issue/PR claim lifecycle: branch creation, worktree allocation,
    isolation verification, and cleanup.

    Parameters
    ----------
    wh_client:
        Optional bridge to the ``wh`` binary for a future R2 worktree CLI.
        **Not used for mutations on this branch** — kept for forward-compat
        when ``wh worktree create|remove`` exists.  Git ops use sandboxed
        ``git`` subprocesses until then.
    allowed_owners:
        Optional owner allowlist.  When ``None``, loads ``WH_ALLOWED_OWNERS``
        (comma-separated).  When empty, any *identity-matched* owner is
        accepted (origin still must match requested owner/repo unless
        ``WH_ALLOW_UNVERIFIED_REMOTE`` is set).
    worktree_base:
        Root directory for worktree allocation.  Defaults to
        ``$WH_WORKTREE_BASE`` or the platform data dir.
    repo_root:
        Path to the git repository where claim operations run.  Defaults to the
        process current working directory.  Must match requested owner/repo
        when origin is parseable.
    """

    wh_client: WhClient | None = None
    worktree_base: str = field(default_factory=_default_worktree_base)
    repo_root: str = field(default_factory=os.getcwd)
    allowed_owners: frozenset[str] | None = None

    def __post_init__(self) -> None:
        """Resolve bases to absolute paths so git cwd cannot escape the sandbox.

        Relative ``worktree_base`` / ``WH_WORKTREE_BASE`` would otherwise be
        interpreted under ``repo_root`` by ``git worktree add/remove`` while
        sandbox checks use the orchestrator CWD — resolve both eagerly.
        """
        self.worktree_base = os.path.abspath(os.path.expanduser(self.worktree_base))
        self.repo_root = os.path.abspath(os.path.expanduser(self.repo_root))
        if self.allowed_owners is None:
            self.allowed_owners = _load_allowed_owners_from_env()
        else:
            self.allowed_owners = frozenset(self.allowed_owners)

    def _get_canonical_remote_identity(self) -> tuple[str, str] | None:
        """Resolve the canonical owner/repo from the repo's origin remote URL.

        Returns (owner, repo) tuple or None if the remote cannot be parsed.
        """
        remote_url = self._git("config", "--get", "remote.origin.url", check=False)
        if not remote_url:
            return None
        remote_url = remote_url.strip()
        # Parse GitHub URLs: https://github.com/owner/repo or git@github.com:owner/repo
        patterns = [
            r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$",
        ]
        for pattern in patterns:
            match = re.search(pattern, remote_url, re.IGNORECASE)
            if match:
                owner = match.group(1)
                repo = match.group(2)
                return (owner, repo)
        return None

    def _assert_claim_identity(self, owner: str, repo: str) -> None:
        """Bind claim labels to ``repo_root`` origin and optional owner allowlist.

        Always (when origin is parseable as GitHub owner/repo):
        requested ``owner``/``repo`` must match origin — independent of allowlist.

        When origin cannot be parsed: fail closed unless
        ``WH_ALLOW_UNVERIFIED_REMOTE`` is truthy (1/true/yes).

        When ``allowed_owners`` is non-empty: owner must also be in that set.
        """
        canonical = self._get_canonical_remote_identity()
        if canonical is None:
            allow_unverified = os.environ.get(_ALLOW_UNVERIFIED_REMOTE_ENV, "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            if not allow_unverified:
                raise ClaimError(
                    "Cannot verify repository identity from origin remote URL. "
                    "Set a parseable origin (github.com/owner/repo) or "
                    f"{_ALLOW_UNVERIFIED_REMOTE_ENV}=1 to override."
                )
        else:
            canonical_owner, canonical_repo = canonical
            if canonical_owner != owner or canonical_repo != repo:
                raise ClaimError(
                    f"Identity mismatch: requested {owner}/{repo}, but repository "
                    f"origin resolves to {canonical_owner}/{canonical_repo}. "
                    "Refusing to claim work for a repository with mismatched metadata."
                )

        allowed = self.allowed_owners or frozenset()
        if allowed and owner not in allowed:
            raise ClaimError(
                f"Owner {owner!r} is not in the configured allowlist "
                f"({sorted(allowed)}). Set WH_ALLOWED_OWNERS or "
                "ClaimManager(allowed_owners=...) for other owners."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def claim_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        base_ref: str = "origin/main",
    ) -> ClaimResult:
        """Claim a GitHub issue by creating a dedicated branch and worktree.

        Steps:
        1. Derive branch name (``hive/gh-{number}``).
        2. Derive worktree path (``{base}/{owner}/{repo}/gh-{number}``).
        3. Fail if worktree already exists (one claim per job).
        4. Create the branch from ``base_ref`` (no tracking of the base).
        5. Create the worktree.
        6. Verify isolation (HEAD matches expected branch).

        Parameters
        ----------
        owner:
            GitHub repository owner (e.g. ``"acme"``).
        repo:
            GitHub repository name (e.g. ``"example-repo"``).
        issue_number:
            The GitHub issue number.
        base_ref:
            Git ref to branch from.  Defaults to ``origin/main``.

        Returns
        -------
        ClaimResult
            Metadata about the created claim.  ``owns_branch`` is ``True``;
            pass ``delete_branch=True`` to :meth:`cleanup` when tearing down.

        Raises
        ------
        ClaimExistsError
            If a worktree for this job already exists.
        ClaimError
            If branch or worktree creation fails.
        """
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        self._assert_claim_identity(owner, repo)
        _validate_ref("base_ref", base_ref)

        branch = f"hive/gh-{issue_number}"
        _validate_ref("branch", branch)
        job_id = f"gh-{issue_number}"
        worktree_path = self._derive_path(owner, repo, job_id)

        self._assert_not_claimed(worktree_path)
        self._create_branch(branch, base_ref)
        try:
            self._create_worktree(worktree_path, branch)
            self._verify_isolation(worktree_path, branch)
            # Publish + set upstream so plain `git push` works in the worktree.
            self._publish_branch(branch)
        except Exception:
            self._remove_worktree(worktree_path)
            self._delete_branch(branch)
            raise

        return ClaimResult(
            owner=owner,
            repo=repo,
            job_id=job_id,
            branch=branch,
            worktree_path=worktree_path,
            issue_number=issue_number,
            owns_branch=True,
        )

    def claim_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        head_branch: str,
        head_repo: str,
        head_sha: str,
    ) -> ClaimResult:
        """Claim a GitHub PR by checking out its head commit in a worktree.

        The PR head is fetched from ``head_repo`` into a collision-free ref,
        verified against ``head_sha``, and the worktree is created from that ref.
        A local branch ``head_branch`` is used only when it does not already
        exist or already points to the expected commit; otherwise the claim is
        rejected.

        Parameters
        ----------
        owner:
            GitHub repository owner.
        repo:
            GitHub repository name.
        pr_number:
            The GitHub PR number.
        head_branch:
            The PR's real head branch name (from GitHub API ``head.ref``).
        head_repo:
            Git remote name or clone URL for the PR head repository.
        head_sha:
            Expected full SHA of the PR head commit.

        Returns
        -------
        ClaimResult
            Metadata about the created claim.  ``owns_branch`` is ``False``;
            :meth:`cleanup` will not delete the PR head by default.

        Raises
        ------
        ClaimExistsError
            If a worktree for this job already exists.
        ClaimError
            If the PR head cannot be fetched, the SHA mismatches, or worktree
            creation fails.
        """
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        self._assert_claim_identity(owner, repo)

        _validate_head_repo(head_repo)
        _validate_sha(head_sha)

        branch = head_branch
        _validate_ref("branch", branch)
        job_id = f"pr-{pr_number}"
        worktree_path = self._derive_path(owner, repo, job_id)

        self._assert_not_claimed(worktree_path)
        fetched_ref = self._fetch_pr_head(pr_number, head_repo, head_branch, head_sha)
        upstream_ref = (
            self._ensure_origin_tracking_ref(head_branch, head_sha)
            if head_repo == "origin"
            else fetched_ref
        )
        try:
            self._create_worktree(worktree_path, branch, ref=fetched_ref)
            self._sync_worktree_to_remote(
                worktree_path, branch, upstream_ref=upstream_ref, pin_sha=head_sha
            )
            self._verify_isolation(worktree_path, branch, expected_sha=head_sha)
        except Exception:
            self._remove_worktree(worktree_path)
            raise

        return ClaimResult(
            owner=owner,
            repo=repo,
            job_id=job_id,
            branch=branch,
            worktree_path=worktree_path,
            pr_number=pr_number,
            owns_branch=False,
        )

    def verify_isolation(self, worktree_path: str, expected_branch: str) -> bool:
        """Verify that a worktree is on the expected branch.

        Call this before any edits to enforce the isolation rule.

        Returns ``True`` if the branch matches.

        Raises
        ------
        IsolationError
            If the branch does not match or cannot be determined.
        """
        return self._verify_isolation(worktree_path, expected_branch)

    def cleanup(
        self,
        worktree_path_or_claim: str | ClaimResult,
        branch: str | None = None,
        *,
        delete_branch: bool = False,
        prune: bool = True,
        owns_branch: bool = False,
    ) -> None:
        """Remove a worktree and optionally delete the local branch.

        Accepts either a :class:`ClaimResult` (recommended) or a verified
        ``worktree_path``/``branch`` pair.  Branch deletion is only performed
        when ``owns_branch`` is ``True`` (for issue claims) and the worktree
        removal succeeded; PR heads are never deleted unless ownership is
        explicitly set.
        """
        if isinstance(worktree_path_or_claim, ClaimResult):
            claim = worktree_path_or_claim
            worktree_path = claim.worktree_path
            branch = claim.branch
            owns_branch = claim.owns_branch
        else:
            if branch is None:
                raise TypeError("cleanup() requires a branch string when passing a path")
            worktree_path = worktree_path_or_claim

        self._assert_under_base(worktree_path)

        if delete_branch and not owns_branch:
            raise ClaimError(
                f"Refusing to delete {branch!r}: ownership not verified. "
                "Pass a ClaimResult or set owns_branch=True for branches this job created."
            )

        removed = self._remove_worktree(worktree_path)
        if not removed:
            if delete_branch:
                raise ClaimError(f"Failed to remove worktree {worktree_path!r}; aborting cleanup")
            return
        if delete_branch and owns_branch:
            self._delete_branch(branch)
        if prune:
            self._prune_worktrees()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _derive_path(self, owner: str, repo: str, job_id: str) -> str:
        """Derive the sandboxed worktree path: ``{base}/{owner}/{repo}/{job_id}``."""
        for name, value in [("owner", owner), ("repo", repo), ("job_id", job_id)]:
            _validate_segment(name, value)
        path = str(Path(self.worktree_base) / owner / repo / job_id)
        self._assert_under_base(path)
        return path

    def _assert_under_base(self, worktree_path: str) -> None:
        """Reject paths that escape ``worktree_base`` after resolve."""
        try:
            base = Path(self.worktree_base).resolve(strict=False)
            candidate = Path(worktree_path).resolve(strict=False)
            candidate.relative_to(base)
        except ValueError as exc:
            raise ClaimError(
                f"Worktree path escapes sandbox base {self.worktree_base!r}: {worktree_path!r}"
            ) from exc

    def _assert_not_claimed(self, worktree_path: str) -> None:
        """Fail if a worktree already exists at the given path or in git's list."""
        if os.path.isdir(worktree_path):
            raise ClaimExistsError(
                f"Worktree already exists at {worktree_path}. "
                "Clean up the existing worktree before reclaiming."
            )
        listed = self._git("worktree", "list", "--porcelain", check=False)
        if listed:
            target = str(Path(worktree_path).resolve(strict=False))
            for line in listed.splitlines():
                if line.startswith("worktree "):
                    existing = line[len("worktree ") :].strip()
                    try:
                        if str(Path(existing).resolve(strict=False)) == target:
                            raise ClaimExistsError(
                                f"Worktree already registered at {worktree_path}. "
                                "Clean up the existing worktree before reclaiming."
                            )
                    except ClaimExistsError:
                        raise
                    except OSError:
                        continue

    def _assert_branch_not_claimed(self, branch: str, worktree_path: str) -> None:
        """Fail if ``branch`` is already checked out in another worktree."""
        listed = self._git("worktree", "list", "--porcelain", check=False)
        if not listed:
            return
        target = str(Path(worktree_path).resolve(strict=False))
        current_block: dict[str, str] = {}
        for line in listed.splitlines():
            if not line:
                if current_block:
                    wt_path = current_block.get("worktree", "")
                    wt_branch = current_block.get("branch", "")
                    if wt_branch == f"refs/heads/{branch}" and wt_path != target:
                        raise ClaimExistsError(
                            f"Branch {branch!r} is already checked out in worktree {wt_path}. "
                            "Defer the claim or remove the existing worktree."
                        )
                current_block = {}
                continue
            if " " in line:
                key, value = line.split(" ", 1)
                current_block[key] = value
        if current_block:
            wt_path = current_block.get("worktree", "")
            wt_branch = current_block.get("branch", "")
            if wt_branch == f"refs/heads/{branch}" and wt_path != target:
                raise ClaimExistsError(
                    f"Branch {branch!r} is already checked out in worktree {wt_path}. "
                    "Defer the claim or remove the existing worktree."
                )

    def _create_branch(self, branch: str, base_ref: str) -> None:
        """Create a new branch from ``base_ref`` without tracking the base.

        Using ``--no-track`` ensures ``push.default=simple`` will push the new
        branch to its own remote ref rather than attempting to update the base
        (e.g. ``origin/main``).  Refs are placed after ``--`` so option-looking
        values cannot be interpreted as flags.  Callers must follow with
        :meth:`_publish_branch` so an upstream is configured for plain push.

        TODO: Route through `wh branch create` when available.
        """
        _validate_ref("branch", branch)
        _validate_ref("base_ref", base_ref)
        # Temporary: direct git mutation until wh branch create is available
        self._git("branch", "--no-track", "--", branch, base_ref)

    def _publish_branch(self, branch: str) -> None:
        """Push a new issue branch and set ``origin/<branch>`` as upstream.

        Required so agents can run plain ``git push`` from the worktree without
        manually configuring tracking after ``branch --no-track``.

        TODO: Route through `wh branch push` when available.
        """
        _validate_ref("branch", branch)
        # Temporary: direct git mutation until wh branch push is available
        self._git("push", "-u", "origin", "--", branch)

    def _ensure_branch_exists(self, branch: str) -> None:
        """Ensure a branch exists and is refreshed from origin when possible.

        Always attempts ``git fetch origin <branch>`` so babysit cycles do not
        claim a stale local tip.  When the local branch is missing, materialize
        it from the fetched remote ref with tracking configured.

        When the local tip is behind origin but ``git branch -f`` fails (branch
        checked out elsewhere), the failure is recorded via a raised
        :class:`ClaimError` only if the remote tip cannot be resolved; otherwise
        :meth:`_sync_worktree_to_remote` refreshes the new worktree after add.
        """
        # Always fetch into the remote-tracking ref so origin/<branch> exists
        # even on single-branch clones (plain `git fetch origin <branch>` may
        # only update FETCH_HEAD without creating origin/<branch>).
        remote_ref = f"origin/{branch}"
        refspec = f"refs/heads/{branch}:refs/remotes/origin/{branch}"
        fetched = (
            self._git("fetch", "origin", f"+{refspec}", check=False) is not None
            or self._git("fetch", "origin", branch, check=False) is not None
        )

        local = self._git("rev-parse", "--verify", branch, check=False)
        if local is not None:
            if fetched and self._git("rev-parse", "--verify", remote_ref, check=False) is not None:
                # Fast-forward only: move local tip to origin/<branch> when the
                # local ref is an ancestor of the remote. Never force-reset when
                # the local branch is ahead or has diverged (preserves unpushed
                # babysit commits).
                is_ancestor = self._is_ancestor(branch, remote_ref)
                if is_ancestor:
                    local_sha = (local or "").strip()
                    remote_sha = (
                        self._git("rev-parse", "--verify", remote_ref, check=False) or ""
                    ).strip()
                    if local_sha and remote_sha and local_sha != remote_sha:
                        self._git("branch", "-f", branch, remote_ref, check=False)
                self._configure_branch_upstream(branch, remote_ref)
            return

        # Local branch missing: materialize at the remote tip (single-branch safe).
        remote_tracking = f"refs/remotes/origin/{branch}"
        tip_sha = self._git("rev-parse", "--verify", remote_tracking, check=False)
        if tip_sha is None:
            tip_sha = self._git("rev-parse", "--verify", remote_ref, check=False)
        if tip_sha is not None:
            self._materialize_local_branch(branch, tip_sha.strip(), remote_ref)
            return
        if fetched:
            fetch_head = self._git("rev-parse", "--verify", "FETCH_HEAD", check=False)
            if fetch_head is None:
                raise ClaimError(
                    f"Cannot materialize branch {branch!r}: fetch succeeded but "
                    "FETCH_HEAD is missing."
                )
            tip = fetch_head.strip()
            self._git("update-ref", remote_tracking, tip, check=False)
            self._materialize_local_branch(branch, tip, remote_ref)
            return
        raise ClaimError(
            f"Cannot materialize branch {branch!r}: fetch failed and "
            f"{remote_ref} is missing. Ensure the PR head exists on origin."
        )

    def _materialize_local_branch(self, branch: str, sha: str, upstream: str) -> None:
        """Create a local branch at ``sha`` and configure ``upstream``.

        Uses an explicit commit SHA instead of ``git branch --track origin/<name>``
        so single-branch clones can materialize branches even when the remote
        tip is only available as ``refs/remotes/origin/<name>``.
        """
        _validate_ref("branch", branch)
        result = self._git_run("branch", "--", branch, sha)
        if result.returncode != 0:
            safe_stderr = _sanitize_diagnostic(result.stderr)
            raise ClaimError(
                f"Failed to create local branch {branch!r} at {sha[:12]}: {safe_stderr}"
            )
        self._configure_branch_upstream(branch, upstream)

    def _configure_branch_upstream(
        self,
        branch: str,
        upstream_ref: str,
        *,
        git_dir: str | None = None,
    ) -> None:
        """Set branch upstream; use config fallback for single-branch clones."""
        if upstream_ref.startswith("refs/") and not upstream_ref.startswith("refs/remotes/"):
            return
        if upstream_ref.startswith("refs/remotes/"):
            upstream_ref = upstream_ref.removeprefix("refs/remotes/")

        prefix: list[str] = ["-C", git_dir] if git_dir else []
        result = self._git_run(*prefix, "branch", "-u", upstream_ref, branch)
        if result.returncode == 0:
            return

        remote, _, merge_branch = upstream_ref.partition("/")
        if not remote or not merge_branch:
            return
        self._git(*prefix, "config", f"branch.{branch}.remote", remote, check=False)
        self._git(
            *prefix,
            "config",
            f"branch.{branch}.merge",
            f"refs/heads/{merge_branch}",
            check=False,
        )

    def _ensure_origin_tracking_ref(self, branch: str, sha: str) -> str:
        """Ensure ``origin/<branch>`` exists and points at verified ``sha``.

        Never prefers global ``FETCH_HEAD`` over the pin — concurrent fetches
        can leave a stale FETCH_HEAD that would poison upstream tracking.
        """
        _validate_ref("branch", branch)
        _validate_sha(sha)
        remote_ref = f"origin/{branch}"
        remote_tracking = f"refs/remotes/origin/{branch}"
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        self._git("fetch", "origin", refspec, check=False)
        # Always pin tracking ref to the verified SHA (fetch may have failed
        # or only updated FETCH_HEAD on single-branch clones).
        self._git("update-ref", remote_tracking, sha, check=False)
        tip = self._git("rev-parse", "--verify", remote_tracking, check=False)
        if tip is None or tip.strip() != sha:
            raise ClaimError(
                f"Failed to pin {remote_tracking} to verified SHA {sha[:12]}. "
                "Refusing to use an unverified tracking tip."
            )
        return remote_ref

    def _sync_worktree_to_remote(
        self,
        worktree_path: str,
        branch: str,
        *,
        upstream_ref: str | None = None,
        pin_sha: str | None = None,
    ) -> None:
        """Align the worktree to ``upstream_ref`` (default ``origin/<branch>``).

        Fast-forward only:

        * HEAD equals upstream → set upstream, done.
        * HEAD is an ancestor of upstream (local behind) → ``reset --hard``.
        * upstream is an ancestor of HEAD (local ahead) → preserve unpushed commits.
        * tips diverged → raise :class:`ClaimError`.

        When ``pin_sha`` is provided, ``reset --hard`` operations are only performed
        if ``remote_sha == pin_sha``, preventing the worktree from advancing past a
        verified commit (e.g., a PR head SHA that was explicitly checked during claim).
        """
        remote_ref = upstream_ref or f"origin/{branch}"
        remote_sha = self._git("rev-parse", "--verify", remote_ref, check=False)
        if remote_sha is None:
            return
        remote_sha = remote_sha.strip()
        head_sha = self._git("-C", worktree_path, "rev-parse", "HEAD", check=False)
        if head_sha is None:
            # Only reset if pin_sha is not set or remote_sha matches pin_sha
            if pin_sha is None or remote_sha == pin_sha:
                self._git("-C", worktree_path, "reset", "--hard", remote_ref)
            self._set_upstream_if_remote(worktree_path, branch, remote_ref)
            return
        head_sha = head_sha.strip()
        if head_sha == remote_sha:
            self._set_upstream_if_remote(worktree_path, branch, remote_ref)
            return

        local_is_ancestor_of_remote = self._is_ancestor("HEAD", remote_ref, git_dir=worktree_path)
        remote_is_ancestor_of_local = self._is_ancestor(remote_ref, "HEAD", git_dir=worktree_path)

        if local_is_ancestor_of_remote:
            # Only reset if pin_sha is not set or remote_sha matches pin_sha
            if pin_sha is None or remote_sha == pin_sha:
                self._git("-C", worktree_path, "reset", "--hard", remote_ref)
            self._set_upstream_if_remote(worktree_path, branch, remote_ref)
            return

        if remote_is_ancestor_of_local:
            self._set_upstream_if_remote(worktree_path, branch, remote_ref)
            return

        # Diverged histories: never hard-reset and abandon local work.
        raise ClaimError(
            f"Claimed worktree at {worktree_path} has diverged from {remote_ref} "
            f"(HEAD={head_sha[:12]}, remote={remote_sha[:12]}). "
            "Refusing to hard-reset and discard local commits. "
            "Push, rebase, or remove the local branch before reclaiming."
        )

    def _set_upstream_if_remote(
        self,
        worktree_path: str,
        branch: str,
        upstream_ref: str,
    ) -> None:
        """Set branch upstream when ``upstream_ref`` is a remote-tracking branch."""
        self._configure_branch_upstream(branch, upstream_ref, git_dir=worktree_path)

    def _create_worktree(
        self,
        worktree_path: str,
        branch: str,
        *,
        ref: str | None = None,
    ) -> None:
        """Create a git worktree at ``worktree_path`` on ``branch``.

        When ``ref`` is provided, the worktree is created from that commit-ish
        and the branch is created if it does not already exist.  If the branch
        already exists, it must point to the same commit as ``ref`` and not be
        checked out in another worktree.
        """
        self._assert_under_base(worktree_path)
        _validate_ref("branch", branch)
        self._assert_branch_not_claimed(branch, worktree_path)
        parent = os.path.dirname(worktree_path)
        os.makedirs(parent, exist_ok=True)
        # TODO: route through `wh worktree create --repo <path>
        # <owner> <repo> <job_id> <branch>` when claim lifecycle is fully
        # covered by the CLI on this branch.

        if ref is None:
            self._git("worktree", "add", "--", worktree_path, branch)
            return

        fetched_sha = self._git("rev-parse", "--verify", ref, check=False)
        if fetched_sha is None:
            raise ClaimError(f"Cannot resolve PR head ref {ref!r}")
        fetched_sha = fetched_sha.strip()

        local = self._git("rev-parse", "--verify", branch, check=False)
        if local is not None:
            local_sha = local.strip()
            if local_sha != fetched_sha:
                raise ClaimError(
                    f"Local branch {branch!r} points to {local_sha[:12]}, "
                    f"but the PR head ref {ref!r} is {fetched_sha[:12]}. "
                    "Refusing to claim a branch with stale or mismatched upstream tracking. "
                    "Create a fresh collision-free claim branch instead."
                )
            # Branch exists and matches: explicitly retarget upstream before worktree add
            # to prevent reusing stale tracking refs
            self._git("branch", "-u", ref, branch, check=False)
            self._git("worktree", "add", "--", worktree_path, branch)
            return

        self._git("worktree", "add", "-b", branch, worktree_path, ref)

    def _fetch_pr_head(
        self,
        pr_number: int,
        head_repo: str,
        head_branch: str,
        head_sha: str,
    ) -> str:
        """Fetch the PR head from ``head_repo`` into a collision-free ref.

        Returns the local ref name containing the verified PR head commit.
        """
        fetched_ref = f"refs/worktrees-hives/pr-{pr_number}/head"
        refspec = f"+refs/heads/{head_branch}:{fetched_ref}"
        result = self._git_run("fetch", head_repo, refspec)
        if result.returncode != 0:
            # Redact credentials from head_repo and stderr before exposing
            safe_repo = _redact_repo_url(head_repo)
            safe_stderr = _sanitize_diagnostic(result.stderr)
            raise ClaimError(
                f"Failed to fetch PR head from {safe_repo!r} "
                f"(exit {result.returncode}): {safe_stderr}"
            )

        fetched = self._git("rev-parse", "--verify", fetched_ref, check=False)
        if fetched is None:
            raise ClaimError(f"Fetched PR ref {fetched_ref!r} did not resolve")
        fetched = fetched.strip()
        if fetched != head_sha:
            raise ClaimError(
                f"PR head SHA mismatch for #{pr_number}: "
                f"expected {head_sha[:12]}, got {fetched[:12]}. "
                "Refusing to claim a mismatched head."
            )
        return fetched_ref

    def _remove_worktree(self, worktree_path: str) -> bool:
        """Remove a git worktree after sandbox validation.

        Paths outside ``worktree_base`` raise :class:`ClaimError`.  Returns
        ``True`` if the removal command succeeded, ``False`` otherwise.
        """
        self._assert_under_base(worktree_path)
        # TODO: prefer `wh worktree remove <path> --force` when available on this branch.
        result = self._git_run("worktree", "remove", "--force", "--", worktree_path)
        return result.returncode == 0

    def _delete_branch(self, branch: str) -> None:
        """Delete a local branch (ignores errors if branch doesn't exist).

        TODO: Route through `wh branch delete` when available.
        """
        _validate_ref("branch", branch)
        # Temporary: direct git mutation until wh branch delete is available
        self._git("branch", "-D", "--", branch, check=False)

    def _prune_worktrees(self) -> None:
        """Prune stale worktree references.

        TODO: Route through `wh worktree prune` when available.
        """
        # Temporary: direct git mutation until wh worktree prune is available
        self._git("worktree", "prune", check=False)

    def _verify_isolation(
        self,
        worktree_path: str,
        expected_branch: str,
        *,
        expected_sha: str | None = None,
    ) -> bool:
        """Verify HEAD branch (and optional commit) in the worktree."""
        actual = self._git(
            "-C",
            worktree_path,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            check=False,
        )
        if actual is None:
            raise IsolationError(f"Cannot determine HEAD in worktree {worktree_path!r}")
        actual = actual.strip()
        if actual != expected_branch:
            raise IsolationError(
                f"Branch mismatch in worktree {worktree_path!r}: "
                f"expected {expected_branch!r}, got {actual!r}"
            )
        if expected_sha is not None:
            head = self._git("-C", worktree_path, "rev-parse", "HEAD", check=False)
            if head is None:
                raise IsolationError(f"Cannot determine HEAD SHA in worktree {worktree_path!r}")
            if head.strip() != expected_sha:
                raise IsolationError(
                    f"SHA mismatch in worktree {worktree_path!r}: "
                    f"expected {expected_sha[:12]}, got {head.strip()[:12]}"
                )
        return True

    def _git_run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a git subprocess command in ``repo_root`` and return the result.

        ``check`` is always ``False`` so callers can inspect exit codes directly.
        """
        cmd = ["git", *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                cwd=self.repo_root,
            )
        except subprocess.TimeoutExpired as exc:
            # Don't stringify TimeoutExpired directly (may expose command args)
            raise ClaimError(f"git command timed out after {exc.timeout}s") from exc
        except FileNotFoundError as exc:
            raise ClaimError(f"git command failed: {exc}") from exc

    def _git(self, *args: str, check: bool = True) -> str | None:
        """Run a git subprocess command in ``repo_root``.

        Returns stdout on success, ``None`` on failure when ``check=False``.

        For ops that already include ``-C <path>`` (e.g. isolation checks
        against a worktree), the leading ``-C`` targets that path; the process
        is still started with ``cwd=repo_root`` as a stable sandbox.
        """
        result = self._git_run(*args)
        if result.returncode == 0:
            return result.stdout
        if check:
            # Sanitize command args and stderr before exposing
            safe_args = " ".join(_sanitize_diagnostic(arg, max_len=50) for arg in args)
            safe_stderr = _sanitize_diagnostic(result.stderr)
            raise ClaimError(f"git {safe_args} failed (exit {result.returncode}): {safe_stderr}")
        return None

    def _is_ancestor(
        self,
        ancestor: str,
        descendant: str,
        git_dir: str | None = None,
    ) -> bool:
        """Return ``True`` if ``ancestor`` is an ancestor of ``descendant``.

        Raises :class:`ClaimError` for missing refs or other git errors.
        ``git_dir`` may be passed as ``-C`` to target a worktree.
        """
        args: list[str] = []
        if git_dir is not None:
            args.extend(["-C", git_dir])
        args.extend(["merge-base", "--is-ancestor", ancestor, descendant])
        result = self._git_run(*args)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        # Sanitize diagnostics before exposing
        safe_ancestor = _sanitize_diagnostic(ancestor, max_len=50)
        safe_descendant = _sanitize_diagnostic(descendant, max_len=50)
        safe_stderr = _sanitize_diagnostic(result.stderr)
        raise ClaimError(
            f"git merge-base --is-ancestor {safe_ancestor} {safe_descendant} failed "
            f"(exit {result.returncode}): {safe_stderr}"
        )


# ------------------------------------------------------------------
# Validation helpers (mirrors Rust wh-core paths::validate_segment)
# ------------------------------------------------------------------


def _validate_segment(field_name: str, value: str) -> None:
    """Reject path-traversal and empty segments."""
    if not value or value in (".", ".."):
        raise ClaimError(f"Invalid {field_name} segment: {value!r}")
    if "/" in value or "\\" in value or ":" in value:
        raise ClaimError(f"Invalid {field_name} segment (contains separator): {value!r}")
    if value.startswith("-"):
        raise ClaimError(f"Invalid {field_name} segment (option-looking): {value!r}")


def _validate_ref(field_name: str, value: str) -> None:
    """Reject empty/option-looking refs; accept any name Git allows as a branch.

    Uses ``git check-ref-format --branch`` so valid names like ``feature/foo+bar``
    are accepted while ``--force``-style values are still rejected.
    """
    if not value or value.startswith("-"):
        raise ClaimError(
            f"Invalid {field_name} {value!r}: must be a plain git ref, "
            "not empty or option-looking (e.g. --force)"
        )
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", value],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ClaimError(f"Cannot validate {field_name}: git unavailable ({exc})") from exc
    if result.returncode != 0:
        raise ClaimError(
            f"Invalid {field_name} {value!r}: rejected by git check-ref-format --branch"
        )


def _validate_sha(value: str) -> None:
    """Reject empty or malformed commit SHAs."""
    if not value:
        raise ClaimError("head_sha must be a non-empty commit SHA")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9A-F]{40}", value):
        raise ClaimError(f"Invalid head_sha {value!r}: expected a 40-character hex SHA")


def _validate_head_repo(value: str) -> None:
    """Accept a remote name or git URL; reject path-like and option-looking values."""
    if not value or value.startswith("-"):
        raise ClaimError(f"Invalid head_repo {value!r}: must be a remote or URL")
    # Remote name (no path separators, no URL scheme)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        return
    # Common git remote URL forms
    if value.startswith(("https://", "http://", "git@", "ssh://", "git://")):
        return
    raise ClaimError(
        f"Invalid head_repo {value!r}: expected a remote name or git URL "
        "(https://, git@, ssh://), not a bare filesystem path"
    )


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert text to a branch-safe slug."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")
