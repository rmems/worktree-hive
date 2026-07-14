//! Durable watched-job state and atomic persistence.
//!
//! The `watched.json` store is implemented by GitHub #26.

use std::fs;
use std::path::PathBuf;

use crate::status::JobStatus;

/// Resolve the platform default user data directory.
///
/// - Windows: `%APPDATA%` (fallback: `%USERPROFILE%\AppData\Roaming`)
/// - macOS: `~/Library/Application Support`
/// - Unix/Linux: `$XDG_DATA_HOME` or `~/.local/share`
fn user_data_dir() -> PathBuf {
    #[cfg(windows)]
    {
        if let Some(appdata) = std::env::var_os("APPDATA") {
            return PathBuf::from(appdata);
        }
        if let Some(profile) = std::env::var_os("USERPROFILE") {
            return PathBuf::from(profile).join("AppData").join("Roaming");
        }
        std::env::temp_dir()
    }

    #[cfg(target_os = "macos")]
    {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home)
                .join("Library")
                .join("Application Support");
        }
        std::env::temp_dir()
    }

    #[cfg(not(any(windows, target_os = "macos")))]
    {
        if let Some(xdg) = std::env::var_os("XDG_DATA_HOME") {
            return PathBuf::from(xdg);
        }
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(".local").join("share");
        }
        std::env::temp_dir()
    }
}

/// Resolve the path to the watched-jobs state file.
///
/// Honours `WH_STATE_PATH` if set; otherwise defaults to
/// `{user_data_dir}/worktrees-hives/watched.json`.
fn state_path() -> PathBuf {
    if let Ok(custom) = std::env::var("WH_STATE_PATH") {
        return PathBuf::from(custom);
    }
    user_data_dir().join("worktrees-hives").join("watched.json")
}

/// Return all currently watched jobs from the persisted store.
///
/// Returns `Ok(vec![])` when the state file does not exist.
/// Returns `Err` when the file exists but cannot be read or parsed,
/// so callers can surface the failure instead of silently masking it.
/// Persistence write path is implemented by GitHub #26.
pub fn load_jobs() -> Result<Vec<JobStatus>, String> {
    let path = state_path();
    let data = match fs::read_to_string(&path) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(format!("failed to read {}: {}", path.display(), e)),
    };
    serde_json::from_str(&data).map_err(|e| format!("failed to parse {}: {}", path.display(), e))
}

#[cfg(test)]
mod tests {
    use super::user_data_dir;

    #[test]
    fn user_data_dir_is_non_empty() {
        let dir = user_data_dir();
        assert!(!dir.as_os_str().is_empty());
    }
}
