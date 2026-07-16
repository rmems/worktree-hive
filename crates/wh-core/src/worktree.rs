use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::error::{Error, Result};
use crate::paths::{canonicalize_for_tools, derive_worktree_path, worktree_base_path};

/// Result of a worktree creation operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Worktree {
    /// Absolute path to the created worktree.
    pub path: PathBuf,
    /// The branch name associated with this worktree.
    pub branch: String,
    /// The repository root this worktree is linked to.
    pub repo_root: PathBuf,
}

/// Manages isolated git worktrees for hive jobs.
#[derive(Debug, Default)]
pub struct WorktreeManager {
    base_path: Option<PathBuf>,
}

impl WorktreeManager {
    /// Create a new manager using the default base path.
    pub fn new() -> Result<Self> {
        let base = worktree_base_path()?;
        Self::with_base(base)
    }

    /// Create a new manager with an explicit base path (for testing or overrides).
    ///
    /// The base is created if missing and stored in canonical form so OS path
    /// aliases (e.g. macOS `/var` → `/private/var`) do not trip sandbox checks.
    pub fn with_base(base: PathBuf) -> Result<Self> {
        fs::create_dir_all(&base).map_err(|e| Error::Io {
            context: "create worktree base directory",
            source: e,
        })?;
        // Canonicalize for OS aliases (macOS /var → /private/var) but strip
        // Windows `\\?\` so `git worktree add` accepts the path.
        let base = canonicalize_for_tools(&base).map_err(|e| Error::Io {
            context: "canonicalize worktree base directory",
            source: e,
        })?;
        Ok(Self {
            base_path: Some(base),
        })
    }

    /// Get the base path this manager uses.
    pub fn base_path(&self) -> Result<&Path> {
        self.base_path.as_deref().ok_or_else(|| Error::Io {
            context: "worktree base path not initialized",
            source: std::io::Error::new(std::io::ErrorKind::NotFound, "base path not set"),
        })
    }

    /// Create a new worktree for the given job.
    ///
    /// The worktree path will be: `{base}/{owner}/{repo}/{job_id}`.
    /// The branch must already exist in the source repository (or will be created from the current HEAD).
    pub fn create(
        &self,
        repo_root: &Path,
        owner: &str,
        repo: &str,
        job_id: &str,
        branch: &str,
    ) -> Result<Worktree> {
        // Reject option-looking branch names (would be parsed as git flags).
        if branch.is_empty() || branch.starts_with('-') {
            return Err(Error::GitCommand {
                args: vec!["worktree".into(), "add".into()],
                stderr: format!("invalid branch name (empty or option-looking): {branch:?}"),
            });
        }

        let base = self.base_path()?;
        let worktree_path = derive_worktree_path(base, owner, repo, job_id)?;

        // Reject symlink *segments under the base* (not OS aliases above the base
        // such as macOS /var → /private/var). Pre-check existing segments, mkdir,
        // then re-check so we never follow a planted escape link.
        reject_symlink_components_under(base, &worktree_path)?;
        if let Some(parent) = worktree_path.parent() {
            fs::create_dir_all(parent).map_err(|e| Error::Io {
                context: "create worktree parent directories",
                source: e,
            })?;
            reject_symlink_components_under(base, parent)?;
        }

        // Verify the repo_root is a valid git repository
        if !repo_root.join(".git").exists() && !is_bare_repo(repo_root)? {
            return Err(Error::GitCommand {
                args: vec!["worktree".into(), "add".into()],
                stderr: format!("not a git repository: {}", repo_root.display()),
            });
        }

        // Create branch from HEAD only if missing; remember for rollback on failure.
        let branch_exists = branch_exists_in_repo(repo_root, branch)?;
        let created_branch = if !branch_exists {
            create_branch(repo_root, branch)?;
            true
        } else {
            false
        };

        // Run `git worktree add` (path and branch after `--`).
        let output = Command::new("git")
            .arg("-C")
            .arg(repo_root)
            .arg("worktree")
            .arg("add")
            .arg("--")
            .arg(&worktree_path)
            .arg(branch)
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git worktree add",
                source: e,
            })?;

        if !output.status.success() {
            if created_branch {
                let _ = Command::new("git")
                    .arg("-C")
                    .arg(repo_root)
                    .arg("branch")
                    .arg("-D")
                    .arg("--")
                    .arg(branch)
                    .output();
            }
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            return Err(Error::GitCommand {
                args: vec![
                    "worktree".into(),
                    "add".into(),
                    worktree_path.to_string_lossy().to_string(),
                    branch.into(),
                ],
                stderr,
            });
        }

        Ok(Worktree {
            path: worktree_path,
            branch: branch.to_string(),
            repo_root: repo_root.to_path_buf(),
        })
    }

    /// List all hive worktrees under the base path.
    pub fn list(&self) -> Result<Vec<Worktree>> {
        let base = self.base_path()?;
        let mut worktrees = Vec::new();

        if !base.exists() {
            return Ok(worktrees);
        }

        // Walk the base directory: {base}/{owner}/{repo}/{job_id}
        for owner_entry in fs::read_dir(base).map_err(|e| Error::Io {
            context: "read worktree base directory",
            source: e,
        })? {
            let owner_entry = owner_entry.map_err(|e| Error::Io {
                context: "read owner entry",
                source: e,
            })?;
            if !owner_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }

            for repo_entry in fs::read_dir(owner_entry.path()).map_err(|e| Error::Io {
                context: "read repo directory",
                source: e,
            })? {
                let repo_entry = repo_entry.map_err(|e| Error::Io {
                    context: "read repo entry",
                    source: e,
                })?;
                if !repo_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                    continue;
                }

                for job_entry in fs::read_dir(repo_entry.path()).map_err(|e| Error::Io {
                    context: "read job directory",
                    source: e,
                })? {
                    let job_entry = job_entry.map_err(|e| Error::Io {
                        context: "read job entry",
                        source: e,
                    })?;
                    if !job_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                        continue;
                    }

                    // Try to get the branch name from the worktree
                    let branch = get_worktree_branch(&job_entry.path())
                        .unwrap_or_else(|_| "unknown".to_string());

                    worktrees.push(Worktree {
                        path: job_entry.path(),
                        branch,
                        repo_root: PathBuf::new(), // Not tracked in list
                    });
                }
            }
        }

        Ok(worktrees)
    }

    /// Remove a worktree by its path.
    ///
    /// If `force` is true, the worktree is removed even if it has uncommitted changes.
    /// The associated branch is NOT deleted by default.
    pub fn remove(&self, worktree_path: &Path, force: bool) -> Result<()> {
        // Verify the path is within our sandbox
        let base = self.base_path()?;
        if !is_within_base(worktree_path, base)? {
            return Err(Error::SandboxViolation {
                base: base.to_path_buf(),
                candidate: worktree_path.to_path_buf(),
                reason: "worktree path is outside configured base",
            });
        }

        // Find the repo root for this worktree
        let repo_root = find_repo_root_for_worktree(worktree_path)?;

        let mut args = vec!["worktree".into(), "remove".into()];
        if force {
            args.push("--force".into());
        }
        args.push("--".into());
        args.push(worktree_path.to_string_lossy().to_string());

        let output = Command::new("git")
            .arg("-C")
            .arg(&repo_root)
            .args(&args)
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git worktree remove",
                source: e,
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            return Err(Error::GitCommand {
                args: args.iter().map(|s| s.to_string()).collect(),
                stderr,
            });
        }

        // Also clean up empty parent directories
        cleanup_empty_parents(worktree_path, base);

        Ok(())
    }

    /// Prune worktree administrative files (stale entries).
    pub fn prune(&self, repo_root: &Path) -> Result<()> {
        let output = Command::new("git")
            .arg("-C")
            .arg(repo_root)
            .arg("worktree")
            .arg("prune")
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git worktree prune",
                source: e,
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            return Err(Error::GitCommand {
                args: vec!["worktree".into(), "prune".into()],
                stderr,
            });
        }

        Ok(())
    }
}

/// Check if a path is a bare git repository.
fn is_bare_repo(path: &Path) -> Result<bool> {
    let output = Command::new("git")
        .arg("-C")
        .arg(path)
        .arg("rev-parse")
        .arg("--is-bare-repository")
        .output()
        .map_err(|e| Error::Io {
            context: "check bare repository",
            source: e,
        })?;

    Ok(output.status.success() && String::from_utf8_lossy(&output.stdout).trim() == "true")
}

/// Check if a branch exists in the repository.
fn branch_exists_in_repo(repo_root: &Path, branch: &str) -> Result<bool> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo_root)
        .arg("show-ref")
        .arg("--verify")
        .arg("--quiet")
        .arg(format!("refs/heads/{branch}"))
        .output()
        .map_err(|e| Error::Io {
            context: "check branch existence",
            source: e,
        })?;

    Ok(output.status.success())
}

/// Create a new branch from HEAD in the repository.
fn create_branch(repo_root: &Path, branch: &str) -> Result<()> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo_root)
        .arg("branch")
        .arg(branch)
        .output()
        .map_err(|e| Error::Io {
            context: "create branch",
            source: e,
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(Error::GitCommand {
            args: vec!["branch".into(), branch.into()],
            stderr,
        });
    }

    Ok(())
}

/// Get the branch name associated with a worktree.
fn get_worktree_branch(worktree_path: &Path) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(worktree_path)
        .arg("rev-parse")
        .arg("--abbrev-ref")
        .arg("HEAD")
        .output()
        .map_err(|e| Error::Io {
            context: "get worktree branch",
            source: e,
        })?;

    if !output.status.success() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--abbrev-ref".into(), "HEAD".into()],
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Find the repository root for a given worktree path.
fn find_repo_root_for_worktree(worktree_path: &Path) -> Result<PathBuf> {
    let output = Command::new("git")
        .arg("-C")
        .arg(worktree_path)
        .arg("rev-parse")
        .arg("--git-common-dir")
        .output()
        .map_err(|e| Error::Io {
            context: "find repo root for worktree",
            source: e,
        })?;

    if !output.status.success() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--git-common-dir".into()],
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    let git_dir = String::from_utf8_lossy(&output.stdout).into_owned();
    let git_dir_path = PathBuf::from(git_dir.trim());

    // For worktrees, the common dir is the main repo's .git directory.
    // The repo root is the parent of .git (never panic on malformed paths).
    if git_dir_path.ends_with(".git") {
        git_dir_path
            .parent()
            .map(|p| p.to_path_buf())
            .ok_or_else(|| Error::GitCommand {
                args: vec!["rev-parse".into(), "--git-common-dir".into()],
                stderr: format!("invalid git directory path: {}", git_dir_path.display()),
            })
    } else {
        // Bare repo case
        Ok(git_dir_path)
    }
}

/// Reject symlink components of `path` that lie *under* `base`.
///
/// Components of `base` itself (and ancestors) are not checked: on macOS the
/// system temp root lives under `/var` → `/private/var`, which is a legitimate
/// OS alias, not an escape. Escape risk is owner/repo/job segments that are
/// symlinks pointing outside the sandbox.
fn reject_symlink_components_under(base: &Path, path: &Path) -> Result<()> {
    let relative = path
        .strip_prefix(base)
        .map_err(|_| Error::SandboxViolation {
            base: base.to_path_buf(),
            candidate: path.to_path_buf(),
            reason: "path is not under worktree base",
        })?;

    let mut cur = base.to_path_buf();
    for comp in relative.components() {
        cur.push(comp);
        if !cur.exists() {
            continue;
        }
        let meta = fs::symlink_metadata(&cur).map_err(|e| Error::Io {
            context: "stat path component for symlink check",
            source: e,
        })?;
        if meta.file_type().is_symlink() {
            return Err(Error::SandboxViolation {
                base: base.to_path_buf(),
                candidate: cur,
                reason: "symlink component under worktree base is not allowed",
            });
        }
    }
    Ok(())
}

/// Check if a path is within the base directory (sandbox check).
fn is_within_base(path: &Path, base: &Path) -> Result<bool> {
    let canonical_path = canonicalize_for_tools(path).map_err(|e| Error::Io {
        context: "canonicalize candidate path",
        source: e,
    })?;
    let canonical_base = canonicalize_for_tools(base).map_err(|e| Error::Io {
        context: "canonicalize base path",
        source: e,
    })?;

    Ok(canonical_path.starts_with(&canonical_base))
}

/// Clean up empty parent directories up to the base.
fn cleanup_empty_parents(path: &Path, base: &Path) {
    let mut current = path.parent();
    while let Some(parent) = current {
        if parent == base {
            break;
        }
        // Only remove if empty
        if fs::read_dir(parent)
            .map(|mut d| d.next().is_none())
            .unwrap_or(false)
        {
            let _ = fs::remove_dir(parent);
            current = parent.parent();
        } else {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    fn init_test_repo(dir: &Path) -> Result<PathBuf> {
        // Initialize a git repo
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("init")
            .arg("-b")
            .arg("main")
            .output()
            .map_err(|e| Error::Io {
                context: "git init",
                source: e,
            })?;

        // Configure git for testing
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("config")
            .arg("user.email")
            .arg("test@example.com")
            .output()
            .map_err(|e| Error::Io {
                context: "git config email",
                source: e,
            })?;

        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("config")
            .arg("user.name")
            .arg("Test User")
            .output()
            .map_err(|e| Error::Io {
                context: "git config name",
                source: e,
            })?;

        // Create initial commit
        fs::write(dir.join("README.md"), "# Test Repo\n").unwrap();
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("add")
            .arg("README.md")
            .output()
            .map_err(|e| Error::Io {
                context: "git add",
                source: e,
            })?;

        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("commit")
            .arg("-m")
            .arg("Initial commit")
            .output()
            .map_err(|e| Error::Io {
                context: "git commit",
                source: e,
            })?;

        Ok(dir.to_path_buf())
    }

    #[test]
    fn create_and_list_worktree() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base.clone()).unwrap();

        // Create a worktree for a new branch
        let wt = manager
            .create(&repo_root, "acme", "test-repo", "job-1", "feature/test")
            .unwrap();

        assert!(wt.path.exists());
        assert_eq!(wt.branch, "feature/test");

        // List worktrees
        let listed = manager.list().unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].path, wt.path);
    }

    #[test]
    fn create_worktree_with_existing_branch() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        // Create a branch in the repo
        Command::new("git")
            .arg("-C")
            .arg(&repo_root)
            .arg("branch")
            .arg("existing-branch")
            .output()
            .unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base).unwrap();

        let wt = manager
            .create(&repo_root, "acme", "test-repo", "job-2", "existing-branch")
            .unwrap();

        assert!(wt.path.exists());
        assert_eq!(wt.branch, "existing-branch");
    }

    #[test]
    fn remove_worktree() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base.clone()).unwrap();

        let wt = manager
            .create(&repo_root, "acme", "test-repo", "job-3", "feature/remove")
            .unwrap();

        assert!(wt.path.exists());

        manager.remove(&wt.path, false).unwrap();

        assert!(!wt.path.exists());
    }

    #[test]
    fn reject_path_outside_sandbox() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base).unwrap();

        // Try to create a worktree with path traversal in job_id
        let result = manager.create(&repo_root, "acme", "test-repo", "../escape", "branch");
        assert!(result.is_err());
    }

    #[test]
    fn prune_worktrees() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base).unwrap();

        let wt = manager
            .create(&repo_root, "acme", "test-repo", "job-4", "feature/prune")
            .unwrap();

        // Remove the worktree directory manually (simulating stale state)
        fs::remove_dir_all(&wt.path).unwrap();

        // Prune should clean up the git worktree admin files
        manager.prune(&repo_root).unwrap();
    }

    #[test]
    fn reject_symlink_owner_under_base() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let outside = temp.path().join("outside");
        fs::create_dir_all(&outside).unwrap();
        let manager = WorktreeManager::with_base(base.clone()).unwrap();

        // Pre-create owner segment as a symlink that escapes the base.
        let owner_link = manager.base_path().unwrap().join("acme");
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, &owner_link).unwrap();
        }
        #[cfg(windows)]
        {
            std::os::windows::fs::symlink_dir(&outside, &owner_link).unwrap();
        }

        let result = manager.create(&repo_root, "acme", "test-repo", "job-sym", "branch-sym");
        assert!(
            matches!(result, Err(Error::SandboxViolation { .. })),
            "expected SandboxViolation, got {result:?}"
        );
    }
}
