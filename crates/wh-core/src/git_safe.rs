//! Allowlisted git and GitHub CLI operations.
//!
//! Safety invariants enforced at the Rust core boundary:
//! - Only allowlisted git subcommands may be executed.
//! - Bare `--force` / `-f` is always rejected; only `--force-with-lease` is permitted.
//! - Merge is blocked only when it is the git subcommand (branch names like `merge` are allowed).
//! - `gh pr merge` and merge-related flags are blocked; `gh api` is not allowlisted.
//! - Mutating commands verify the current branch when `expected_branch` is provided to `run`.
//! - All policy violations carry stable structured error codes.

use std::collections::HashSet;
use std::path::Path;
use std::process::Command;

use crate::error::{Error, PolicyCode, Result};

/// Git subcommands allowed for hive jobs.
const ALLOWED_GIT_SUBCOMMANDS: &[&str] = &[
    "add",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "config",
    "diff",
    "fetch",
    "log",
    "ls-files",
    "ls-remote",
    "merge-base",
    "mv",
    "pull",
    "push",
    "rebase",
    "remote",
    "reset",
    "restore",
    "rev-parse",
    "rm",
    "show",
    "stash",
    "status",
    "switch",
    "tag",
];

/// Git subcommands that mutate branch state and require branch verification.
const MUTATING_SUBCOMMANDS: &[&str] = &[
    "add",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "config",
    "mv",
    "pull",
    "push",
    "rebase",
    "remote",
    "reset",
    "restore",
    "rm",
    "stash",
    "switch",
    "tag",
];

/// GitHub CLI subcommands allowed for hive jobs.
///
/// Note: `api` is intentionally excluded so merge-related REST/GraphQL cannot be
/// invoked through `gh api` (e.g. `mergePullRequest` / REST merge endpoints).
const ALLOWED_GH_SUBCOMMANDS: &[&str] = &[
    "auth", "browse", "gist", "issue", "label", "pr", "release", "repo", "secret", "ssh-key",
    "variable", "workflow",
];

/// `gh pr` sub-subcommands that are blocked (merge is disallowed).
const BLOCKED_GH_PR_SUBSUBCOMMANDS: &[&str] = &["merge", "ready"];

/// `gh pr` flags that are blocked (direct merge-related flags).
const BLOCKED_GH_FLAGS: &[&str] = &["--merge", "--squash", "--rebase", "--auto", "--admin"];

/// Pre-validated git command ready for execution.
#[derive(Debug, Clone)]
pub struct SafeGitCommand {
    args: Vec<String>,
}

/// Output from executing a safe git or gh command.
#[derive(Debug, Clone, serde::Serialize)]
pub struct GitOutput {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

impl SafeGitCommand {
    /// Create a new safe git command after validating the full argument list.
    ///
    /// Returns an error if the command violates any safety policy.
    pub fn new(args: &[String]) -> Result<Self> {
        if args.is_empty() {
            return Err(Error::PolicyViolation {
                code: PolicyCode::SubcommandNotAllowed,
                message: "no git subcommand provided".to_owned(),
            });
        }

        let subcommand = &args[0];

        // Reject merge only when it is the git subcommand (not a branch/ref named "merge").
        if is_merge_subcommand(subcommand) {
            return Err(Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                message: format!("merge is not allowed: `git {}`", args.join(" ")),
            });
        }

        // Validate subcommand against allowlist.
        let allowed: HashSet<&str> = ALLOWED_GIT_SUBCOMMANDS.iter().copied().collect();
        if !allowed.contains(subcommand.as_str()) {
            return Err(Error::PolicyViolation {
                code: PolicyCode::SubcommandNotAllowed,
                message: format!("git subcommand `{subcommand}` is not on the allowlist"),
            });
        }

        // Reject ANY bare --force / -f always; only --force-with-lease is allowed.
        // Even when both appear together, bare force is still rejected.
        if args.iter().any(|a| is_bare_force_flag(a)) {
            return Err(Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                message: "bare --force/-f is not allowed; use --force-with-lease only".to_owned(),
            });
        }

        // Git push also accepts force via `+<src>:<dst>` refspecs. Treat those as
        // bare force pushes so supervised jobs cannot rewrite refs without an explicit
        // lease-protected flag.
        if subcommand == "push" && args.iter().skip(1).any(|a| is_force_refspec(a)) {
            return Err(Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                message: "force-push refspecs prefixed with `+` are not allowed; use --force-with-lease only".to_owned(),
            });
        }

        if subcommand == "push" && args.iter().any(|a| a == "--mirror") {
            return Err(Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                message: "git push --mirror is not allowed; use --force-with-lease only".to_owned(),
            });
        }

        if subcommand == "pull" {
            let has_safe = args
                .iter()
                .any(|a| a == "--rebase" || a.starts_with("--rebase=") || a == "--ff-only");
            if !has_safe {
                return Err(Error::PolicyViolation {
                    code: PolicyCode::MergeBlocked,
                    message: "git pull requires --rebase or --ff-only under hive policy".to_owned(),
                });
            }
        }

        if subcommand == "rebase"
            && args
                .iter()
                .any(|a| a == "--exec" || a == "-x" || a.starts_with("--exec="))
        {
            return Err(Error::PolicyViolation {
                code: PolicyCode::SubcommandNotAllowed,
                message: "git rebase --exec/-x is not allowed under hive policy".to_owned(),
            });
        }

        reject_external_write_targets(subcommand, &args[1..])?;

        Ok(Self {
            args: args.to_vec(),
        })
    }

    /// The validated git subcommand.
    #[must_use]
    pub fn subcommand(&self) -> &str {
        &self.args[0]
    }

    /// Whether this command requires branch verification before execution.
    #[must_use]
    pub fn requires_branch_check(&self) -> bool {
        let allowed: HashSet<&str> = MUTATING_SUBCOMMANDS.iter().copied().collect();
        allowed.contains(self.subcommand())
    }

    /// Verify that the current branch matches the expected job branch.
    ///
    /// Resolves the current branch from the repository at `repo_dir` and compares it
    /// against `expected_branch`. Returns `Ok(())` on match, error otherwise.
    pub fn verify_branch(&self, repo_dir: &Path, expected_branch: &str) -> Result<()> {
        let current = resolve_current_branch(repo_dir)?;
        if current != expected_branch {
            return Err(Error::PolicyViolation {
                code: PolicyCode::BranchMismatch,
                message: format!(
                    "current branch `{current}` does not match expected `{expected_branch}`"
                ),
            });
        }
        Ok(())
    }

    /// Execute the validated git command in `repo_dir`.
    ///
    /// When `expected_branch` is `Some` and this command is mutating, verifies the
    /// current branch before running.
    pub fn run(&self, repo_dir: &Path, expected_branch: Option<&str>) -> Result<GitOutput> {
        if self.requires_branch_check() {
            if let Some(expected) = expected_branch {
                self.verify_branch(repo_dir, expected)?;
            }
        }

        let output = Command::new("git")
            .arg("-C")
            .arg(repo_dir)
            .args(&self.args)
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git",
                source: e,
            })?;

        Ok(GitOutput {
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            exit_code: output.status.code().unwrap_or(1),
        })
    }

    /// Return the full argument list (for display / logging).
    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.args
    }
}

/// Pre-validated GitHub CLI command ready for execution.
#[derive(Debug, Clone)]
pub struct SafeGhCommand {
    args: Vec<String>,
}

impl SafeGhCommand {
    /// Create a new safe gh command after validating the full argument list.
    ///
    /// Returns an error if the command violates any safety policy.
    pub fn new(args: &[String]) -> Result<Self> {
        if args.is_empty() {
            return Err(Error::PolicyViolation {
                code: PolicyCode::GhSubcommandNotAllowed,
                message: "no gh subcommand provided".to_owned(),
            });
        }

        let subcommand = &args[0];

        // Validate subcommand against allowlist.
        let allowed: HashSet<&str> = ALLOWED_GH_SUBCOMMANDS.iter().copied().collect();
        if !allowed.contains(subcommand.as_str()) {
            return Err(Error::PolicyViolation {
                code: PolicyCode::GhSubcommandNotAllowed,
                message: format!("gh subcommand `{subcommand}` is not on the allowlist"),
            });
        }

        // Block `gh pr merge` / `gh pr ready` even when inherited flags precede the
        // subcommand, e.g. `gh pr -R owner/repo merge 1`.
        if subcommand == "pr" {
            if let Some(pr_sub) = first_positional_after(&args[1..]) {
                let blocked: HashSet<&str> = BLOCKED_GH_PR_SUBSUBCOMMANDS.iter().copied().collect();
                if blocked.contains(pr_sub) {
                    return Err(Error::PolicyViolation {
                        code: PolicyCode::MergeBlocked,
                        message: format!("`gh pr {pr_sub}` is not allowed"),
                    });
                }
            }
        }

        // Block merge-related flags anywhere in the argument list.
        let blocked_flags: HashSet<&str> = BLOCKED_GH_FLAGS.iter().copied().collect();
        for arg in &args[1..] {
            if blocked_flags.contains(arg.as_str()) {
                return Err(Error::PolicyViolation {
                    code: PolicyCode::GhFlagNotAllowed,
                    message: format!("gh flag `{arg}` is not allowed"),
                });
            }
        }

        Ok(Self {
            args: args.to_vec(),
        })
    }

    /// Execute the validated gh command, returning stdout, stderr, and exit code.
    pub fn run(&self) -> Result<GitOutput> {
        let output = Command::new("gh")
            .args(&self.args)
            .output()
            .map_err(|e| Error::Io {
                context: "spawn gh",
                source: e,
            })?;

        Ok(GitOutput {
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            exit_code: output.status.code().unwrap_or(1),
        })
    }

    /// Return the full argument list (for display / logging).
    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.args
    }
}

/// Resolve the current branch name from a repository working tree.
fn resolve_current_branch(repo_dir: &Path) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo_dir)
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .output()
        .map_err(|e| Error::Io {
            context: "resolve current branch",
            source: e,
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(Error::PolicyViolation {
            code: PolicyCode::GitDirUnavailable,
            message: format!("failed to resolve current branch: {}", stderr.trim()),
        });
    }

    let branch = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if branch.is_empty() || branch == "HEAD" {
        return Err(Error::PolicyViolation {
            code: PolicyCode::GitDirUnavailable,
            message: "current branch name is empty (detached HEAD?)".to_owned(),
        });
    }

    Ok(branch)
}

/// First non-flag positional argument, skipping common `gh` inherited options that take values.
fn first_positional_after(args: &[String]) -> Option<&str> {
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        if a == "--" {
            return args.get(i + 1).map(String::as_str);
        }
        if a.starts_with('-') {
            // Only documented parent flags; anything else fails closed.
            if a == "--help" || a == "-h" {
                i += 1;
                continue;
            }
            if a == "-R" || a == "--repo" {
                i += 2;
                continue;
            }
            if a.starts_with("--repo=") {
                i += 1;
                continue;
            }
            return None;
        }
        return Some(a);
    }
    None
}

/// Whether a validated `gh` command mutates local checkout/worktree state.
#[must_use]
pub fn gh_requires_branch_check(args: &[String]) -> bool {
    if args.first().map(String::as_str) != Some("pr") {
        return false;
    }
    let Some(pr_sub) = first_positional_after(&args[1..]) else {
        return false;
    };
    matches!(
        pr_sub,
        "checkout" | "create" | "close" | "reopen" | "edit" | "ready" | "merge" | "review"
    )
}

fn reject_external_write_targets(subcommand: &str, args: &[String]) -> Result<()> {
    match subcommand {
        "clone" => {
            let positionals: Vec<&str> = args
                .iter()
                .map(String::as_str)
                .filter(|a| !a.starts_with('-') && *a != "--")
                .collect();
            if positionals.len() >= 2 {
                let dest = positionals[1];
                let abs = dest.starts_with('/')
                    || (dest.len() > 2 && dest.as_bytes().get(1) == Some(&b':'));
                if abs || dest.contains("..") {
                    return Err(Error::PolicyViolation {
                        code: PolicyCode::PathNotAllowed,
                        message: format!(
                            "git clone destination `{dest}` must be a relative path under the worktree"
                        ),
                    });
                }
            }
        }
        "config" => {
            if args.iter().any(|a| a == "--global" || a == "--system") {
                return Err(Error::PolicyViolation {
                    code: PolicyCode::PathNotAllowed,
                    message: "git config --global/--system is not allowed under hive policy"
                        .to_owned(),
                });
            }
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                if a == "-f" || a == "--file" {
                    if let Some(path) = args.get(i + 1) {
                        let abs = path.starts_with('/')
                            || (path.len() > 2 && path.as_bytes().get(1) == Some(&b':'));
                        if abs || path.contains("..") {
                            return Err(Error::PolicyViolation {
                                code: PolicyCode::PathNotAllowed,
                                message: format!(
                                    "git config file `{path}` must stay under the worktree"
                                ),
                            });
                        }
                    }
                }
                if let Some(path) = a.strip_prefix("--file=") {
                    if path.starts_with('/') || path.contains("..") {
                        return Err(Error::PolicyViolation {
                            code: PolicyCode::PathNotAllowed,
                            message: format!(
                                "git config file `{path}` must stay under the worktree"
                            ),
                        });
                    }
                }
                i += 1;
            }
        }
        _ => {}
    }
    Ok(())
}

/// First positional target of `checkout`/`switch` (branch/ref name).
#[must_use]
pub fn checkout_or_switch_target(args: &[String]) -> Option<&str> {
    if args.is_empty() {
        return None;
    }
    let sub = args[0].as_str();
    if sub != "checkout" && sub != "switch" {
        return None;
    }
    let mut i = 1;
    while i < args.len() {
        let a = args[i].as_str();
        if a == "--" {
            return args.get(i + 1).map(String::as_str);
        }
        if a.starts_with('-') {
            if matches!(a, "-b" | "-B" | "-c" | "-C" | "--orphan" | "--track" | "-t") {
                return args.get(i + 1).map(String::as_str);
            }
            if a.starts_with("--") && a.contains('=') {
                i += 1;
                continue;
            }
            i += 1;
            continue;
        }
        return Some(a);
    }
    None
}

fn is_merge_subcommand(subcommand: &str) -> bool {
    matches!(subcommand, "merge" | "mergetool")
}

/// True for bare force flags that are never allowed.
///
/// `--force-with-lease` and `--force-with-lease=<ref>` are allowed and must not match.
fn is_bare_force_flag(arg: &str) -> bool {
    if arg == "-f" || arg == "--force" {
        return true;
    }
    // Reject `--force=...` but not `--force-with-lease` / `--force-with-lease=...`.
    arg.starts_with("--force=")
}

fn is_force_refspec(arg: &str) -> bool {
    arg.starts_with('+') && arg.len() > 1
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- git allowlist tests ----

    #[test]
    fn allowed_subcommand_passes() {
        let cmd = SafeGitCommand::new(&["status".to_owned()]).unwrap();
        assert_eq!(cmd.subcommand(), "status");
    }

    #[test]
    fn push_passes() {
        let cmd = SafeGitCommand::new(&["push".to_owned()]).unwrap();
        assert_eq!(cmd.subcommand(), "push");
    }

    #[test]
    fn unknown_subcommand_rejected() {
        let err = SafeGitCommand::new(&["gc".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::SubcommandNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn empty_args_rejected() {
        let err = SafeGitCommand::new(&[]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::SubcommandNotAllowed,
                ..
            }
        ));
    }

    // ---- merge tests ----

    #[test]
    fn merge_subcommand_rejected() {
        let err = SafeGitCommand::new(&["merge".to_owned(), "feature".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn mergetool_subcommand_rejected() {
        let err = SafeGitCommand::new(&["mergetool".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn checkout_branch_named_merge_allowed() {
        // Merge detection is subcommand-only; a branch named "merge" is fine.
        let cmd = SafeGitCommand::new(&["checkout".to_owned(), "merge".to_owned()]).unwrap();
        assert_eq!(cmd.subcommand(), "checkout");
        assert_eq!(cmd.args(), &["checkout", "merge"]);
    }

    // ---- force push tests ----

    #[test]
    fn bare_force_rejected() {
        let err = SafeGitCommand::new(&["push".to_owned(), "--force".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn bare_f_flag_rejected() {
        let err = SafeGitCommand::new(&["push".to_owned(), "-f".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn force_with_lease_accepted() {
        let cmd =
            SafeGitCommand::new(&["push".to_owned(), "--force-with-lease".to_owned()]).unwrap();
        assert_eq!(cmd.subcommand(), "push");
    }

    #[test]
    fn force_with_lease_and_bare_force_rejected() {
        // Bare --force is always rejected, even when --force-with-lease is also present.
        let err = SafeGitCommand::new(&[
            "push".to_owned(),
            "--force".to_owned(),
            "--force-with-lease".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn force_equals_form_rejected() {
        let err = SafeGitCommand::new(&["push".to_owned(), "--force=true".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn push_mirror_rejected() {
        let err = SafeGitCommand::new(&[
            "push".to_owned(),
            "--mirror".to_owned(),
            "origin".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn pull_without_rebase_or_ff_only_rejected() {
        let err = SafeGitCommand::new(&["pull".to_owned(), "origin".to_owned(), "main".to_owned()])
            .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn pull_with_rebase_allowed() {
        SafeGitCommand::new(&[
            "pull".to_owned(),
            "--rebase".to_owned(),
            "origin".to_owned(),
            "main".to_owned(),
        ])
        .unwrap();
    }

    #[test]
    fn rebase_exec_rejected() {
        let err = SafeGitCommand::new(&[
            "rebase".to_owned(),
            "-x".to_owned(),
            "true".to_owned(),
            "main".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::SubcommandNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn clone_absolute_dest_rejected() {
        let err = SafeGitCommand::new(&[
            "clone".to_owned(),
            "https://example.com/r.git".to_owned(),
            "/tmp/outside".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::PathNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn config_global_rejected() {
        let err = SafeGitCommand::new(&[
            "config".to_owned(),
            "--global".to_owned(),
            "user.name".to_owned(),
            "x".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::PathNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn force_refspec_rejected() {
        let err = SafeGitCommand::new(&[
            "push".to_owned(),
            "origin".to_owned(),
            "+HEAD:main".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    // ---- mutating subcommand detection ----

    #[test]
    fn push_is_mutating() {
        let cmd = SafeGitCommand::new(&["push".to_owned()]).unwrap();
        assert!(cmd.requires_branch_check());
    }

    #[test]
    fn commit_is_mutating() {
        let cmd =
            SafeGitCommand::new(&["commit".to_owned(), "-m".to_owned(), "msg".to_owned()]).unwrap();
        assert!(cmd.requires_branch_check());
    }

    #[test]
    fn add_is_mutating() {
        let cmd = SafeGitCommand::new(&["add".to_owned(), ".".to_owned()]).unwrap();
        assert!(cmd.requires_branch_check());
    }

    #[test]
    fn clean_is_mutating() {
        let cmd = SafeGitCommand::new(&["clean".to_owned(), "-fd".to_owned()]).unwrap();
        assert!(cmd.requires_branch_check());
    }

    #[test]
    fn status_is_not_mutating() {
        let cmd = SafeGitCommand::new(&["status".to_owned()]).unwrap();
        assert!(!cmd.requires_branch_check());
    }

    #[test]
    fn diff_is_not_mutating() {
        let cmd = SafeGitCommand::new(&["diff".to_owned()]).unwrap();
        assert!(!cmd.requires_branch_check());
    }

    // ---- gh allowlist tests ----

    #[test]
    fn gh_pr_create_allowed() {
        let cmd = SafeGhCommand::new(&[
            "pr".to_owned(),
            "create".to_owned(),
            "--title".to_owned(),
            "test".to_owned(),
        ])
        .unwrap();
        assert_eq!(cmd.args(), &["pr", "create", "--title", "test"]);
    }

    #[test]
    fn gh_pr_merge_after_repo_flag_rejected() {
        let err = SafeGhCommand::new(&[
            "pr".to_owned(),
            "-R".to_owned(),
            "acme/widgets".to_owned(),
            "merge".to_owned(),
            "1".to_owned(),
            "-m".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn gh_pr_merge_rejected() {
        let err = SafeGhCommand::new(&["pr".to_owned(), "merge".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn gh_pr_ready_rejected() {
        let err = SafeGhCommand::new(&["pr".to_owned(), "ready".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn gh_merge_flag_rejected() {
        let err = SafeGhCommand::new(&["pr".to_owned(), "create".to_owned(), "--merge".to_owned()])
            .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::GhFlagNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn gh_api_rejected() {
        // api removed from allowlist to block merge via REST/GraphQL.
        let err = SafeGhCommand::new(&[
            "api".to_owned(),
            "graphql".to_owned(),
            "-f".to_owned(),
            "query=mutation { mergePullRequest }".to_owned(),
        ])
        .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::GhSubcommandNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn gh_unknown_subcommand_rejected() {
        let err = SafeGhCommand::new(&["codespace".to_owned()]).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::GhSubcommandNotAllowed,
                ..
            }
        ));
    }

    #[test]
    fn gh_issue_list_allowed() {
        let cmd = SafeGhCommand::new(&["issue".to_owned(), "list".to_owned()]).unwrap();
        assert_eq!(cmd.args(), &["issue", "list"]);
    }

    // ---- error display tests ----

    #[test]
    fn policy_code_display() {
        assert_eq!(PolicyCode::BareForcePush.as_str(), "BARE_FORCE_PUSH");
        assert_eq!(PolicyCode::MergeBlocked.as_str(), "MERGE_BLOCKED");
        assert_eq!(
            PolicyCode::SubcommandNotAllowed.as_str(),
            "SUBCOMMAND_NOT_ALLOWED"
        );
        assert_eq!(PolicyCode::BranchMismatch.as_str(), "BRANCH_MISMATCH");
        assert_eq!(
            PolicyCode::GhSubcommandNotAllowed.as_str(),
            "GH_SUBCOMMAND_NOT_ALLOWED"
        );
        assert_eq!(PolicyCode::GhFlagNotAllowed.as_str(), "GH_FLAG_NOT_ALLOWED");
    }

    #[test]
    fn error_display_includes_code_and_message() {
        let err = Error::PolicyViolation {
            code: PolicyCode::BareForcePush,
            message: "test message".to_owned(),
        };
        let display = format!("{err}");
        assert!(display.contains("BARE_FORCE_PUSH"));
        assert!(display.contains("test message"));
    }

    /// Isolated git repo with a named branch for execution tests.
    ///
    /// Avoids depending on the workspace checkout (tarpaulin / detached HEAD).
    fn temp_repo_with_branch(branch: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU64, Ordering};
        static SEQ: AtomicU64 = AtomicU64::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "wh-core-git-safe-{}-{}-{}",
            std::process::id(),
            seq,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        if dir.exists() {
            let _ = std::fs::remove_dir_all(&dir);
        }
        std::fs::create_dir_all(&dir).expect("create temp repo dir");

        let null_dev = if cfg!(windows) { "NUL" } else { "/dev/null" };
        let git = |args: &[&str]| {
            let output = Command::new("git")
                .arg("-C")
                .arg(&dir)
                .args(args)
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", null_dev)
                .output()
                .expect("spawn git");
            assert!(
                output.status.success(),
                "git {args:?} failed in {}: {}",
                dir.display(),
                String::from_utf8_lossy(&output.stderr)
            );
        };

        // Portable across Git versions / Windows template races.
        git(&["init"]);
        git(&["checkout", "-b", branch]);
        git(&["config", "user.email", "test@example.com"]);
        git(&["config", "user.name", "wh-core-test"]);
        std::fs::write(
            dir.join("README"),
            "init
",
        )
        .expect("write README");
        git(&["add", "README"]);
        git(&["commit", "-m", "init"]);
        dir
    }

    #[test]
    fn run_status_in_repo_executes() {
        let repo = temp_repo_with_branch("main");
        let cmd =
            SafeGitCommand::new(&["rev-parse".to_owned(), "--is-inside-work-tree".to_owned()])
                .unwrap();
        let out = cmd.run(&repo, None).expect("git should run in temp repo");
        assert_eq!(out.exit_code, 0, "stderr={}", out.stderr);
        assert_eq!(out.stdout.trim(), "true");
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn run_verifies_expected_branch_for_mutating() {
        let repo = temp_repo_with_branch("job-branch");
        let current = resolve_current_branch(&repo).expect("resolve branch");
        assert_eq!(current, "job-branch");

        // status is not mutating — expected_branch is ignored.
        let cmd = SafeGitCommand::new(&["status".to_owned(), "--porcelain".to_owned()]).unwrap();
        let out = cmd
            .run(&repo, Some("definitely-not-this-branch"))
            .expect("status should run");
        assert_eq!(out.exit_code, 0, "stderr={}", out.stderr);

        // Mutating with wrong branch is rejected before spawn.
        let push = SafeGitCommand::new(&["push".to_owned(), "--dry-run".to_owned()]).unwrap();
        let err = push
            .run(&repo, Some("definitely-not-this-branch"))
            .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BranchMismatch,
                ..
            }
        ));

        // Matching branch passes branch verification (push may still fail without a remote).
        let push_ok = SafeGitCommand::new(&["push".to_owned(), "--dry-run".to_owned()]).unwrap();
        match push_ok.run(&repo, Some("job-branch")) {
            Ok(_) => {}
            Err(Error::PolicyViolation {
                code: PolicyCode::BranchMismatch,
                ..
            }) => panic!("matching branch must not fail branch verification"),
            Err(_) => {} // e.g. no remote configured
        }

        let _ = std::fs::remove_dir_all(&repo);
    }
}
