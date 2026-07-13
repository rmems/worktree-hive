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
