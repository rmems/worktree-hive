# worktrees-hives

`worktrees-hives` is a multi-platform foundation for turning issues into pull requests and babysitting those pull requests with isolated subagents. It combines an agent skill, a Python policy orchestrator, and a Rust safety core.

> [!IMPORTANT]
> The project never auto-merges. It prepares pull requests for a human merge decision.

The repository is in its foundation phase. The Rust workspace is available; the Python package and complete agent skill are tracked separately.

## Architecture

```text
Agent platform / SKILL.md
          |
          v
Python orchestrator (worktrees_hives)
          | CLI + JSON envelope v1
          v
Rust CLI (wh) / wh-core
          |
          v
git / gh / operating system
```

| Layer | Owns | Does not own |
| --- | --- | --- |
| Agent skill | Prompts and guidance for when and how an agent calls the tooling | Enforceable safety policy |
| Python `worktrees_hives` | Discovery, partitioning, issue-to-PR and babysit policy, stack ordering, fix budgets, and reports | Direct worktree or unsafe git mutation |
| Rust `wh-core` + `wh` | Worktrees, durable job state, process supervision, path sandboxing, branch verification, and hard git/GitHub safety stops | High-level agent policy |
| `git`, `gh`, OS | Version-control, GitHub, and process primitives invoked through Rust | Hive policy |

The Python/Rust boundary is CLI-first and uses versioned JSON instead of PyO3. The v1 contract is tracked in [GitHub #40](https://github.com/rmems/worktrees-hives/issues/40); its documentation will live at `docs/json-contract.md`.

## Safety invariants

These rules apply to every agent, platform, and command path:

- **Never merge pull requests.** No `gh pr merge`, merge API, or equivalent automated path is allowed.
- Force pushes may use only `--force-with-lease`; bare `--force` and `-f` are forbidden.
- Each job edits only its assigned branch and isolated worktree.
- Mutating operations must verify the expected job branch and remain inside the configured path sandbox.
- A babysit cycle may create at most **three code-fix commits per PR**. Review replies are not capped.
- Stacked pull requests are handled from the bottom of the stack upward.
- Review replies are posted only after the fix is pushed and include the pushed SHA plus attribution, for example: `Grok Build agent: fixed in abc1234`.

Soft prompt text is not considered enforcement. Hard stops belong in Rust so a malformed prompt or Python bug cannot bypass them.

## Owner allowlist

By default, discovery and mutation are limited to:

- `rmems/*`
- `Limen-Neural/*`

An explicit user override is required for any other owner. Authentication should use the least privileges needed by `git` and `gh`; credentials must never be written to reports or logs.

## Build and install `wh`

Prerequisites:

- Stable Rust from [rustup](https://rustup.rs/)
- Git
- GitHub CLI for future GitHub operations

The workspace MSRV is Rust **1.85**. `rust-toolchain.toml` selects the latest stable toolchain for development and CI.

```bash
cargo build --workspace
cargo test --workspace
cargo install --path crates/wh
wh --help
```

Contributor quality gates:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Python package

The Python bridge is planned in [GitHub #30](https://github.com/rmems/worktrees-hives/issues/30). Once that package lands under `python/`, install it in editable mode with:

```bash
python -m pip install -e ./python
```

Python will invoke `wh` from `WH_BIN` or `PATH` and consume the versioned JSON contract. It will not duplicate Rust-owned state or mutation logic.

## Project documentation

- [`AGENTS.md`](AGENTS.md) — agent roles, boundaries, data flow, and worktree rules
- [`REVIEW.md`](REVIEW.md) — pull-request lifecycle and review checklist
- Hybrid foundation epic: [GitHub #21](https://github.com/rmems/worktrees-hives/issues/21)
- Rust core epic: [GitHub #22](https://github.com/rmems/worktrees-hives/issues/22)
- Python orchestration epic: [GitHub #23](https://github.com/rmems/worktrees-hives/issues/23)
- [Linear `worktrees-hives` project](https://linear.app/rpd-34/project/worktrees-hives-e3052de4caa3)

## License

Licensed under the [Apache License 2.0](LICENSE).
