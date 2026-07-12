//! Durable watched-job state and atomic persistence.
//!
//! The `watched.json` store is implemented by GitHub #26.

use std::fs;
use std::path::PathBuf;

use crate::status::JobStatus;

/// Resolve the path to the watched-jobs state file.
///
/// Honours `WH_STATE_PATH` if set; otherwise defaults to
/// `~/.local/share/worktrees-hives/watched.json`.
fn state_path() -> PathBuf {
    if let Ok(custom) = std::env::var("WH_STATE_PATH") {
        return PathBuf::from(custom);
    }
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let home = std::env::var_os("HOME").unwrap_or_else(|| "/tmp".into());
            PathBuf::from(home).join(".local/share")
        });
    base.join("worktrees-hives/watched.json")
}

/// Return all currently watched jobs from the persisted store.
///
/// Returns an empty list when the state file does not exist or cannot be
/// parsed. Persistence write path is implemented by GitHub #26.
pub fn load_jobs() -> Vec<JobStatus> {
    let path = state_path();
    let data = match fs::read_to_string(&path) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };
    serde_json::from_str(&data).unwrap_or_default()
}
