"""CLI for worktrees-hives Python orchestrator (entry: worktrees-hives / wh-orch).

Does NOT register as `wh` — that name is reserved for the Rust binary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from worktrees_hives.watchlist import (
    CorruptStateError,
    JobState,
    JobStatus,
    PolicyError,
    Watchlist,
)


def _print_job(job: JobState) -> None:
    """Print a job in human-readable format."""
    print(f"  {job.job_id}: {job.owner}/{job.repo} [{job.status.value}]")
    if job.pr_url:
        print(f"    PR: {job.pr_url}")
    if job.residual_blockers:
        print(f"    Blockers: {', '.join(job.residual_blockers)}")
    print(f"    Fixes: {job.fix_count}/{job.max_fixes}")


def _watchlist_from_args(args: argparse.Namespace) -> Watchlist:
    """Build Watchlist from CLI args; honors --state or WH_STATE_PATH default."""
    return Watchlist(Path(args.state) if args.state else None)


def cmd_add(args: argparse.Namespace) -> int:
    """Handle watchlist add command."""
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
        print(f"Added job {job.job_id} to watchlist")
        return 0
    except CorruptStateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except PolicyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_remove(args: argparse.Namespace) -> int:
    """Handle watchlist remove command."""
    try:
        w = _watchlist_from_args(args)
        w.remove(args.job_id)
        print(f"Removed job {args.job_id} from watchlist")
        return 0
    except CorruptStateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    """Handle watchlist list command."""
    try:
        w = _watchlist_from_args(args)
    except CorruptStateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    status_filter = JobStatus(args.status) if args.status else None
    jobs = w.list_jobs(owner=args.owner, repo=args.repo, status=status_filter)
    if not jobs:
        print("No jobs in watchlist")
        return 0
    for job in jobs:
        _print_job(job)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Handle watchlist check command."""
    try:
        w = _watchlist_from_args(args)
    except CorruptStateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    categories = w.check()
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


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point (worktrees-hives / wh-orch)."""
    parser = argparse.ArgumentParser(
        prog="worktrees-hives",
        description=("worktrees-hives Python orchestrator (does not shadow the Rust `wh` binary)"),
    )
    parser.add_argument(
        "--state",
        help=(
            "Path to watched state file (default: WH_STATE_PATH or platform data dir/watched.json)"
        ),
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
    wl_sub.add_parser("check", help="Check jobs and show action needed")

    args = parser.parse_args(argv)

    commands = {
        "watchlist": {
            "add": cmd_add,
            "remove": cmd_remove,
            "list": cmd_list,
            "check": cmd_check,
        }
    }

    if args.command == "watchlist":
        handler = commands["watchlist"][args.wl_command]
        return handler(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
