# AGENTS.md

## Purpose

This file defines how coding agents contribute to `worktrees-hives` and how the future hive runtime divides responsibility. The project is a Python/Rust hybrid designed for multiple agent platforms.

## Non-negotiable safety

- Never merge a pull request or invoke a merge API.
- Never use bare `git push --force` or `git push -f`; only `--force-with-lease` is permitted.
- Never edit outside a job's assigned worktree or branch.
- Default repository scope is `rmems/*` and `Limen-Neural/*`; other owners require an explicit user override.
- Limit code-fix commits to three per PR per babysit cycle. Replies are unlimited.
- Process stacked PRs from the bottom of the stack upward.
- Post review replies only after pushing, and include the pushed SHA plus agent attribution.

Rust must enforce safety-sensitive mutation rules. Skill instructions and Python checks provide defense in depth but are not sufficient on their own.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

## Hybrid architecture

```text
Agent / SKILL.md
       |
       | intent and operator context
       v
Python package: worktrees_hives
       |
       | wh subprocess calls + JSON envelope v1
       v
Rust binary: wh -> wh-core
       |
       | allowlisted subprocess operations
       v
git / gh / operating system
```

| Layer | Responsibilities |
| --- | --- |
| Agent skill | Describe when to discover work, spawn subagents, invoke the orchestrator, babysit PRs, and report results. Prompt content is portable guidance, not a security boundary. |
| Python orchestrator | Discover and partition work, enforce owner and per-cycle policy, order stacks, drive issue-to-PR and babysit loops, and build human-readable reports. |
| Rust core and CLI | Resolve sandboxed paths, create and remove worktrees, persist atomic job state, supervise child processes, verify branches, and reject unsafe git/GitHub operations. |
| External tools | `git` and `gh` perform only operations selected and validated by Rust. The OS supplies filesystem and process primitives. |

The stable cross-language boundary is a CLI with JSON envelopes. PyO3 is out of scope for v1. The contract is versioned independently so Python and Rust can evolve without sharing an in-process ABI.

## Source ownership

### Rust

Rust code lives in `crates/`:

- `crates/wh-core/` is the reusable library and source of truth for worktrees, state, process execution, paths, and safety policy.
- `crates/wh/` is the `wh` command-line adapter. It parses arguments, calls `wh-core`, emits human or JSON output, and maps policy failures to exit code 2.

Keep security boundaries in `wh-core`, not only in the CLI parser. Git must be invoked as a subprocess rather than through libgit2. New mutating commands require branch verification and path-sandbox tests.

### Python

Python code will live in `python/src/worktrees_hives/`:

- The subprocess bridge locates `wh` through `WH_BIN` or `PATH` and validates JSON responses.
- Discovery, partitioning, issue-to-PR, babysit, and reporting modules own high-level policy.
- Python must not reimplement Rust-owned worktree, state, branch, or git safety checks.
- The three-code-fix-commit budget is a Python orchestration rule; Rust still rejects unsafe individual commands.

### Agent skill

The installable `SKILL.md` will own platform-facing prompts and command guidance. It may adapt spawning instructions to a host platform, but it must preserve the same safety invariants and call the Python/Rust boundary instead of bypassing it.

## Data flow

1. The operator or agent supplies GitHub or Linear issue/PR context.
2. Python discovers eligible work under the owner allowlist and partitions independent jobs.
3. Rust allocates `{base}/{owner}/{repo}/{job_id}` and creates the assigned branch worktree.
4. A worker agent changes only that worktree and branch.
5. Rust validates mutations and performs allowlisted `git` or `gh` subprocess calls.
6. Python opens or checks the PR, processes stacks bottom-up, applies the fix budget, and reports residual blockers.
7. After a pushed fix, the agent replies with SHA and attribution.
8. The cycle ends when the PR is merge-ready or blocked. A human remains responsible for any merge.

GitHub is the product issue source. Linear team `rmems` (`RM`) mirrors product planning. Beads tracks session claims, dependencies, and completion locally; it is not a replacement for GitHub product issues.

## Runtime paths and overrides

| Purpose | Default | Override |
| --- | --- | --- |
| Worktree root | User data directory plus `worktrees-hives/worktrees` | `WH_WORKTREE_BASE` |
| Job worktree | `{worktree root}/{owner}/{repo}/{job_id}` | Derived only; must remain sandboxed |
| Watched state | User data directory plus `worktrees-hives/watched.json` | `WH_STATE_PATH` |
| Rust binary used by Python | `wh` from `PATH` | `WH_BIN` |

Use platform-aware user-data resolution in implementation. For example, prefer the platform default application-data location such as `~/.local/share` on Linux, `~/Library/Application Support` on macOS, or `%APPDATA%` on Windows rather than assuming a Linux-only home-directory layout.

## JSON and process boundary

**Status: Planned / not yet implemented.** The features below are documented requirements for the v1 contract (GitHub #40).

Version 1 responses will use this envelope shape:

```json
{"ok":true,"schema_version":1,"command":"state.show","data":{},"error":null}
```

- Standard output will be machine-readable JSON when `--json` is selected.
- Diagnostics will belong on standard error.
- Additive fields will be compatible within v1; removals or semantic renames will require a schema-version change.
- `run-with-timeout` is reserved for the later process-supervisor work and must not be improvised in the foundation CLI.

See GitHub #40 and the planned `docs/json-contract.md` for the complete contract.

## Contribution workflow

1. Run `bd prime`, inspect `bd ready`, and claim the relevant bead before non-trivial work.
2. Read the linked GitHub or Linear issue and preserve its acceptance criteria.
3. Start from an up-to-date base and create a focused feature branch.
4. For runtime jobs, create an isolated worktree before editing. Do not share a writable worktree across agents.
5. Keep Rust, Python, and skill changes within their ownership boundaries.
6. Run the narrowest checks first, then the repository quality gates documented in `README.md`.
7. Update or close Beads accurately, push all commits, and verify the branch is up to date with its remote.
8. Open or update a PR and cross-link the relevant GitHub/Linear issues. Never merge it.

## Review expectations

Use [`REVIEW.md`](REVIEW.md) for the shared checklist. Reviewers should verify behavior at both the soft-policy and hard-enforcement layers, with particular attention to merge prohibition, force-push parsing, expected-branch checks, path traversal, JSON compatibility, and cross-platform path handling.

## Related planning

- Hybrid foundation: GitHub #21
- Rust core: GitHub #22 and #24–#29
- Python orchestration: GitHub #23, #30, and #37–#39
- Hybrid glue and docs: GitHub #40–#42
- Linear project: <https://linear.app/rpd-34/project/worktrees-hives-e3052de4caa3>
