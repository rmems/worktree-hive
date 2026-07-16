"""CLI for worktrees-hives Python orchestrator (entry: worktrees-hives / wh-orch).

Does NOT register as `wh` — that name is reserved for the Rust binary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from worktrees_hives.errors import PolicyError
from worktrees_hives.watchlist import (
    CorruptStateError,
    JobState,
    JobStatus,
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


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point (worktrees-hives / wh-orch)."""
    parser = argparse.ArgumentParser(
        prog="worktrees-hives",
        description=("worktrees-hives Python orchestrator (does not shadow the Rust `wh` binary)"),
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
