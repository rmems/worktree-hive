"""CLI for worktrees-hives Python orchestrator (entry: worktrees-hives / wh-orch).

Does NOT register as `wh` — that name is reserved for the Rust binary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worktrees_hives.babysit import DEFAULT_ATTRIBUTION, MAX_FIX_COMMITS_PER_CYCLE
from worktrees_hives.errors import PolicyError
from worktrees_hives.watchlist import (
    CorruptStateError,
    JobState,
    JobStatus,
    Watchlist,
)

if TYPE_CHECKING:
    from worktrees_hives.babysit import BabysitResult
    from worktrees_hives.discover import DiscoveryResult
    from worktrees_hives.stacks import PRInfo, Stack


def _print_job(job: JobState) -> None:
    """Print a job in human-readable format."""
    print(f"  {job.job_id}: {job.owner}/{job.repo} [{job.status.value}]")
    if job.pr_url:
        print(f"    PR: {job.pr_url}")
    if job.residual_blockers:
        print(f"    Blockers: {', '.join(job.residual_blockers)}")
    print(f"    Fixes: {job.fix_count}/{job.max_fixes}")


def _watchlist_from_args(args: argparse.Namespace) -> Watchlist:
    """Build Watchlist from CLI args; honors --state or WH_WATCHLIST_PATH default."""
    return Watchlist(Path(args.state) if args.state else None)


def _v1_envelope(
    command: str,
    data: dict[str, object],
    *,
    ok: bool = True,
    error: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a v1 CLI JSON envelope (schema_version=1)."""
    return {
        "ok": ok,
        "schema_version": 1,
        "command": command,
        "data": data,
        "error": error,
    }


def _job_to_json(job: JobState) -> dict[str, Any]:
    """Serialize a JobState for JSON envelopes (list/check items)."""
    return {
        "job_id": job.job_id,
        "owner": job.owner,
        "repo": job.repo,
        "branch": job.branch,
        "status": job.status.value,
        "stack_id": job.stack_id,
        "pr_number": job.pr_number,
        "pr_url": job.pr_url,
        "fix_count": job.fix_count,
        "max_fixes": job.max_fixes,
        "fix_budget_remaining": job.fix_budget_remaining,
        "babysit_cycle": job.babysit_cycle,
        "residual_blockers": list(job.residual_blockers),
        "last_check": job.last_check,
        "error": job.error,
    }


# Exact command → error data shape (avoid substring traps like "list" in "watchlist.check").
_CORRUPT_DATA: dict[str, dict[str, object]] = {
    "watchlist.add": {},
    "watchlist.remove": {"job_id": None},
    "watchlist.list": {"jobs": []},
    "watchlist.check": {"categories": {}},
}


def _emit_corrupt(command: str, err: CorruptStateError, *, as_json: bool) -> int:
    print(f"Error: {err}", file=sys.stderr)
    if as_json:
        empty = _CORRUPT_DATA.get(command, {})
        print(
            json.dumps(
                _v1_envelope(
                    command,
                    empty,
                    ok=False,
                    error={"code": "CORRUPT_STATE", "message": str(err)},
                )
            )
        )
    return 1


def cmd_add(args: argparse.Namespace) -> int:
    """Handle watchlist add command."""
    as_json = getattr(args, "json", False)
    try:
        w = _watchlist_from_args(args)
        job = w.add(
            job_id=args.job_id,
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            stack_id=args.stack_id,
            max_fixes=args.max_fixes,
        )
        if as_json:
            print(json.dumps(_v1_envelope("watchlist.add", {"job": _job_to_json(job)})))
        else:
            print(f"Added job {job.job_id} to watchlist")
        return 0
    except CorruptStateError as e:
        return _emit_corrupt("watchlist.add", e, as_json=as_json)
    except PolicyError as e:
        print(f"Error: {e}", file=sys.stderr)
        if as_json:
            print(
                json.dumps(
                    _v1_envelope(
                        "watchlist.add",
                        {},
                        ok=False,
                        error={"code": e.code, "message": e.message},
                    )
                )
            )
        return 2
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        if as_json:
            print(
                json.dumps(
                    _v1_envelope(
                        "watchlist.add",
                        {},
                        ok=False,
                        error={"code": "VALUE_ERROR", "message": str(e)},
                    )
                )
            )
        return 1


def cmd_remove(args: argparse.Namespace) -> int:
    """Handle watchlist remove command."""
    as_json = getattr(args, "json", False)
    try:
        w = _watchlist_from_args(args)
        w.remove(args.job_id)
        if as_json:
            print(
                json.dumps(
                    _v1_envelope("watchlist.remove", {"job_id": args.job_id, "removed": True})
                )
            )
        else:
            print(f"Removed job {args.job_id} from watchlist")
        return 0
    except CorruptStateError as e:
        return _emit_corrupt("watchlist.remove", e, as_json=as_json)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        if as_json:
            print(
                json.dumps(
                    _v1_envelope(
                        "watchlist.remove",
                        {"job_id": args.job_id, "removed": False},
                        ok=False,
                        error={"code": "NOT_FOUND", "message": str(e)},
                    )
                )
            )
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    """Handle watchlist list command."""
    as_json = getattr(args, "json", False)
    try:
        w = _watchlist_from_args(args)
        status_filter = JobStatus(args.status) if args.status else None
        jobs = w.list_jobs(owner=args.owner, repo=args.repo, status=status_filter)
    except CorruptStateError as e:
        return _emit_corrupt("watchlist.list", e, as_json=as_json)
    if as_json:
        print(json.dumps(_v1_envelope("watchlist.list", {"jobs": [_job_to_json(j) for j in jobs]})))
        return 0
    if not jobs:
        print("No jobs in watchlist")
        return 0
    for job in jobs:
        _print_job(job)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Handle watchlist check command."""
    as_json = getattr(args, "json", False)
    try:
        w = _watchlist_from_args(args)
        # check() may stamp last_check and _save(); catch write failures too.
        categories = w.check(owner=args.owner, repo=args.repo)
    except CorruptStateError as e:
        return _emit_corrupt("watchlist.check", e, as_json=as_json)
    if as_json:
        data = {cat: [_job_to_json(j) for j in jobs] for cat, jobs in categories.items()}
        print(json.dumps(_v1_envelope("watchlist.check", {"categories": data})))
        return 0
    has_work = False
    for category, jobs in categories.items():
        if jobs:
            has_work = True
            print(f"\n{category.upper()}:")
            for job in jobs:
                _print_job(job)
    if not has_work:
        print("No jobs in watchlist")
    return 0


# ---------------------------------------------------------------------------
# Orchestration commands (discover / plan / babysit)
# ---------------------------------------------------------------------------


def _fail(command: str, code: str, message: str, *, as_json: bool, exit_code: int) -> int:
    """Report a failure on stderr and, under --json, as a v1 error envelope."""
    print(f"Error: {message}", file=sys.stderr)
    if as_json:
        print(
            json.dumps(
                _v1_envelope(command, {}, ok=False, error={"code": code, "message": message})
            )
        )
    return exit_code


def _guard(command: str, as_json: bool, fn: Any) -> int:
    """Run *fn*, mapping the house exception ladder onto exit codes.

    Mirrors ``cmd_add``: PolicyError → 2 (a safety/allowlist refusal),
    everything else recoverable → 1.
    """
    try:
        return fn()
    except PolicyError as e:
        return _fail(command, e.code, e.message, as_json=as_json, exit_code=2)
    except (ValueError, OSError, RuntimeError) as e:
        return _fail(command, type(e).__name__, str(e), as_json=as_json, exit_code=1)


def _owners_arg(args: argparse.Namespace) -> list[str] | None:
    """Repeatable --owner, or None to fall back to WH_ALLOWED_OWNERS."""
    owners = getattr(args, "owner", None)
    return list(owners) if owners else None


def _split_repo_slug(slug: str) -> tuple[str, str]:
    """Split ``owner/repo``, rejecting anything else."""
    owner, sep, repo = slug.partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise ValueError(f"--repo expects owner/repo, got {slug!r}")
    return owner, repo


def _print_discovery(result: DiscoveryResult) -> None:
    """Human view: grouped by owner/repo, with errors and truncation surfaced."""
    by_repo: dict[str, list[Any]] = {}
    for issue in result.issues:
        by_repo.setdefault(f"{issue.owner}/{issue.repo}", []).append(issue)

    for slug in sorted(by_repo):
        print(f"\n{slug}:")
        for issue in sorted(by_repo[slug], key=lambda i: i.number):
            kind = "PR " if issue.is_pr else "issue"
            labels = f"  [{', '.join(issue.labels)}]" if issue.labels else ""
            print(f"  {kind} #{issue.number}  {issue.title}{labels}")

    print(
        f"\n{len(result.issues)} item(s) across {len(result.owners_scanned)} owner(s): "
        f"{', '.join(result.owners_scanned) or 'none'}"
    )
    # A silently truncated scan is the dangerous failure mode: it looks like a
    # complete picture but hides work.
    if result.truncated:
        print("WARNING: results truncated — more items exist than were fetched", file=sys.stderr)
    for err in result.errors:
        print(f"WARNING: {err}", file=sys.stderr)


def cmd_discover(args: argparse.Namespace) -> int:
    """Discover open issues/PRs across allowed owners (read-only)."""
    as_json = getattr(args, "json", False)

    def run() -> int:
        from worktrees_hives import discover as discover_mod

        result = discover_mod.discover_all(
            owners=_owners_arg(args),
            kind=args.kind,
            allow_non_default_owners=args.allow_non_default_owners,
            check_auth=not args.no_check_auth,
        )
        if as_json:
            # format_for_orchestrator already emits the agreed shape — do not
            # build a second serializer that can drift from it.
            print(
                json.dumps(_v1_envelope("discover", discover_mod.format_for_orchestrator(result)))
            )
        else:
            _print_discovery(result)
        return 0

    return _guard("discover", as_json, run)


def _plan_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Resolve --repo / --owner into a deduped list of (owner, repo)."""
    targets: list[tuple[str, str]] = []
    for slug in getattr(args, "repo", None) or []:
        targets.append(_split_repo_slug(slug))

    owners = getattr(args, "owner", None) or []
    if owners:
        from worktrees_hives.discover import list_repos_for_owner

        for owner in owners:
            repos, error, truncated = list_repos_for_owner(owner)
            if error:
                print(f"WARNING: {owner}: {error}", file=sys.stderr)
            if truncated:
                print(f"WARNING: {owner}: repo list truncated", file=sys.stderr)
            targets.extend((owner, r) for r in repos)

    if not targets:
        raise ValueError("no targets: pass --repo owner/repo and/or --owner")

    seen: set[tuple[str, str]] = set()
    deduped = []
    for target in targets:
        key = (target[0].casefold(), target[1].casefold())
        if key not in seen:
            seen.add(key)
            deduped.append(target)
    return deduped


def _pr_to_json(pr: PRInfo, stack_id: str | None, position: int | None) -> dict[str, Any]:
    return {
        "owner": pr.owner,
        "repo": pr.repo,
        "number": pr.number,
        "head_ref": pr.head_ref,
        "base_ref": pr.base_ref,
        "state": pr.state.value,
        "stack_id": stack_id,
        "stack_position": position,
    }


def _stack_index(stacks: list[Stack]) -> dict[str, tuple[str | None, int | None]]:
    """Map PRInfo.key → (stack_id, stack_position) for annotating the order."""
    index: dict[str, tuple[str | None, int | None]] = {}
    for stack in stacks:
        for member in stack.members:
            index[member.pr.key] = (stack.stack_id, member.stack_position)
    return index


def cmd_plan(args: argparse.Namespace) -> int:
    """Emit the bottom-up processing order for open PRs (read-only)."""
    as_json = getattr(args, "json", False)

    def run() -> int:
        from worktrees_hives.stacks import (
            StackDetector,
            find_standalone_prs,
            order_prs_bottom_up,
        )

        ordered_all: list[dict[str, Any]] = []
        for owner, repo in _plan_targets(args):
            detector = StackDetector(owner=owner, repo=repo)
            prs = detector.fetch_pr_infos(f"{owner}/{repo}")
            stacks = detector.detect_stacks(prs)
            standalone = find_standalone_prs(prs, stacks, allow_unlisted=args.allow_unlisted)
            ordered = order_prs_bottom_up(stacks, standalone, allow_unlisted=args.allow_unlisted)
            index = _stack_index(stacks)
            for pr in ordered:
                stack_id, position = index.get(pr.key, (None, None))
                ordered_all.append(_pr_to_json(pr, stack_id, position))

        if as_json:
            print(json.dumps(_v1_envelope("plan", {"ordered": ordered_all})))
            return 0

        if not ordered_all:
            print("No PRs to process")
            return 0
        print("Processing order (bottom of stack first):")
        for i, entry in enumerate(ordered_all, start=1):
            where = (
                f"  stack {entry['stack_id']} pos {entry['stack_position']}"
                if entry["stack_id"]
                else "  standalone"
            )
            print(
                f"  {i:>3}. {entry['owner']}/{entry['repo']}#{entry['number']}"
                f"  {entry['head_ref']} → {entry['base_ref']}{where}"
            )
        return 0

    return _guard("plan", as_json, run)


def _babysit_result_to_json(result: BabysitResult) -> dict[str, Any]:
    return {
        "pr_number": result.pr_number,
        "state": result.state.value,
        "fixes_applied": getattr(result, "fixes_applied", None),
        "threads_replied": getattr(result, "threads_replied", None),
        "residual_blockers": list(result.residual_blockers),
    }


def cmd_babysit(args: argparse.Namespace) -> int:
    """Run a babysit cycle over PRs in one repo. Never merges."""
    as_json = getattr(args, "json", False)

    def run() -> int:
        from worktrees_hives.babysit import babysit_multiple

        results = babysit_multiple(
            owner=args.owner,
            repo=args.repo,
            pr_numbers=list(args.pr_numbers),
            attribution=args.attribution,
            max_fixes=args.max_fixes,
        )
        if as_json:
            print(
                json.dumps(
                    _v1_envelope(
                        "babysit",
                        {"results": [_babysit_result_to_json(r) for r in results]},
                    )
                )
            )
            return 0

        for result in results:
            print(f"\nPR #{result.pr_number}: {result.state.value}")
            fixes = getattr(result, "fixes_applied", None)
            if fixes is not None:
                print(f"  Fixes applied: {fixes}/{args.max_fixes}")
            for blocker in result.residual_blockers:
                print(f"  Blocker: {blocker}")
        # Merge-ready is not merged; this tool never merges.
        print("\nNo PR was merged — merging remains a human decision.")
        return 0

    return _guard("babysit", as_json, run)


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point (worktrees-hives / wh-orch)."""
    parser = argparse.ArgumentParser(
        prog="worktrees-hives",
        description="worktrees-hives Python orchestrator (does not shadow the Rust `wh` binary)",
    )
    parser.add_argument(
        "--state",
        help=(
            "Path to watchlist state file "
            "(default: WH_WATCHLIST_PATH or platform data dir/watchlist.json; "
            "never WH_STATE_PATH / Rust watched.json)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a v1 JSON envelope on stdout (diagnostics on stderr)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # watchlist commands
    wl = sub.add_parser("watchlist", help="Manage the job watchlist")
    wl_sub = wl.add_subparsers(dest="wl_command", required=True)

    # watchlist add
    add_p = wl_sub.add_parser("add", help="Add a job to the watchlist")
    add_p.add_argument("job_id", help="Unique job identifier")
    add_p.add_argument(
        "owner",
        help="Repository owner (e.g. acme, example-org)",
    )
    add_p.add_argument("repo", help="Repository name")
    add_p.add_argument("branch", help="Branch name")
    add_p.add_argument("--stack-id", help="Stack membership identifier")
    add_p.add_argument("--max-fixes", type=int, default=3, help="Max fix commits (default: 3)")

    # watchlist remove
    rm_p = wl_sub.add_parser("remove", help="Remove a job from the watchlist")
    rm_p.add_argument("job_id", help="Job identifier to remove")

    # watchlist list
    list_p = wl_sub.add_parser("list", help="List watched jobs")
    list_p.add_argument("--owner", help="Filter by owner")
    list_p.add_argument("--repo", help="Filter by repo")
    list_p.add_argument(
        "--status",
        choices=[s.value for s in JobStatus],
        help="Filter by status",
    )

    # watchlist check
    check_p = wl_sub.add_parser("check", help="Check jobs and show action needed")
    check_p.add_argument("--owner", help="Filter by owner")
    check_p.add_argument("--repo", help="Filter by repo")

    # discover
    disc_p = sub.add_parser(
        "discover",
        help="Discover open issues/PRs across allowed owners (read-only)",
    )
    disc_p.add_argument(
        "--owner",
        action="append",
        metavar="OWNER",
        help="Owner to scan; repeatable. Defaults to WH_ALLOWED_OWNERS.",
    )
    disc_p.add_argument(
        "--kind",
        choices=["all", "issues", "prs"],
        default="all",
        help="What to discover (default: all)",
    )
    disc_p.add_argument(
        "--allow-non-default-owners",
        action="store_true",
        help="Scan owners outside the WH_ALLOWED_OWNERS allowlist",
    )
    disc_p.add_argument(
        "--no-check-auth",
        action="store_true",
        help="Skip the `gh auth status` pre-flight check",
    )

    # plan
    plan_p = sub.add_parser(
        "plan",
        help="Show the bottom-up PR processing order (read-only)",
    )
    plan_p.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/REPO",
        help="Repository to plan; repeatable",
    )
    plan_p.add_argument(
        "--owner",
        action="append",
        metavar="OWNER",
        help="Plan every repo for this owner; repeatable",
    )
    plan_p.add_argument(
        "--allow-unlisted",
        action="store_true",
        help="Skip owner allowlist filtering",
    )

    # babysit
    baby_p = sub.add_parser(
        "babysit",
        help="Run a babysit cycle over PRs in one repo (never merges)",
    )
    baby_p.add_argument("--owner", required=True, help="Repository owner")
    baby_p.add_argument("--repo", required=True, help="Repository name")
    baby_p.add_argument(
        "pr_numbers",
        nargs="+",
        type=int,
        metavar="PR",
        help=(
            "PR numbers in bottom-up stack order. Ordering is not computed here: "
            "take it from `worktrees-hives plan`. Out-of-order input defers "
            "children behind a blocked parent incorrectly."
        ),
    )
    baby_p.add_argument(
        "--max-fixes",
        type=int,
        default=MAX_FIX_COMMITS_PER_CYCLE,
        help=(f"Max code-fix commits per PR per cycle (default: {MAX_FIX_COMMITS_PER_CYCLE})"),
    )
    baby_p.add_argument(
        "--attribution",
        default=DEFAULT_ATTRIBUTION,
        help=f"Attribution prefixed to review replies (default: {DEFAULT_ATTRIBUTION!r})",
    )

    args = parser.parse_args(argv)

    if args.command == "watchlist":
        watchlist_handlers = {
            "add": cmd_add,
            "remove": cmd_remove,
            "list": cmd_list,
            "check": cmd_check,
        }
        return watchlist_handlers[args.wl_command](args)

    handlers = {
        "discover": cmd_discover,
        "plan": cmd_plan,
        "babysit": cmd_babysit,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
