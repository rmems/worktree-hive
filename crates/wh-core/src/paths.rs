//! Platform-aware data paths and sandbox validation.

use std::env;
use std::path::{Path, PathBuf};

use crate::error::{Error, Result};

const WORKTREE_BASE_ENV: &str = "WH_WORKTREE_BASE";

/// Resolve the configured worktree base path.
///
/// Uses `WH_WORKTREE_BASE` when set, otherwise:
/// - Unix: `$XDG_DATA_HOME/worktrees-hives/worktrees` or
///   `$HOME/.local/share/worktrees-hives/worktrees`
/// - Windows: `%LOCALAPPDATA%\\worktrees-hives\\worktrees`
pub fn worktree_base_path() -> Result<PathBuf> {
    if let Some(value) = env::var_os(WORKTREE_BASE_ENV) {
        return Ok(PathBuf::from(value));
    }

    default_worktree_base()
}

fn default_worktree_base() -> Result<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        let local = env::var_os("LOCALAPPDATA")
            .or_else(|| env::var_os("APPDATA"))
            .ok_or_else(|| Error::Io {
                context: "resolve LOCALAPPDATA",
                source: std::io::Error::new(
                    std::io::ErrorKind::NotFound,
                    "LOCALAPPDATA is not set",
                ),
            })?;

        return Ok(PathBuf::from(local)
            .join("worktrees-hives")
            .join("worktrees"));
    }

    #[cfg(not(target_os = "windows"))]
    {
        let data_home = env::var_os("XDG_DATA_HOME").map(PathBuf::from).unwrap_or_else(|| {
            PathBuf::from(
                env::var_os("HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("~")),
            )
            .join(".local")
            .join("share")
        });

        Ok(data_home.join("worktrees-hives").join("worktrees"))
    }
}

/// Derive a sandboxed worktree path: `{base}/{owner}/{repo}/{job_id}`.
pub fn derive_worktree_path(base: &Path, owner: &str, repo: &str, job_id: &str) -> Result<PathBuf> {
    validate_segment("owner", owner)?;
    validate_segment("repo", repo)?;
    validate_segment("job_id", job_id)?;

    Ok(base.join(owner).join(repo).join(job_id))
}

pub(crate) fn validate_segment(field: &'static str, value: &str) -> Result<()> {
    if value.is_empty() || value == "." || value == ".." {
        return Err(Error::InvalidSegment {
            field,
            value: value.to_owned(),
        });
    }

    if value.chars().any(std::path::is_separator) {
        return Err(Error::InvalidSegment {
            field,
            value: value.to_owned(),
        });
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::derive_worktree_path;

    #[test]
    fn derives_expected_path() {
        let base = Path::new("/tmp/worktrees");
        let derived = derive_worktree_path(base, "rmems", "worktrees-hives", "wh-347")
            .expect("path should derive");

        assert_eq!(
            derived,
            Path::new("/tmp/worktrees/rmems/worktrees-hives/wh-347")
        );
    }

    #[test]
    fn rejects_invalid_segments() {
        let base = Path::new("/tmp/worktrees");
        assert!(derive_worktree_path(base, "", "repo", "job").is_err());
        assert!(derive_worktree_path(base, ".", "repo", "job").is_err());
        assert!(derive_worktree_path(base, "owner", "..", "job").is_err());
        assert!(derive_worktree_path(base, "owner/repo", "repo", "job").is_err());
    }
}
