//! Durable watched-job state and atomic persistence.
//!
//! The `watched.json` store is implemented by GitHub #26.

use std::fs;
use std::path::Path;

use crate::paths::state_path;
use crate::status::JobStatus;

/// Return all currently watched jobs from the persisted store.
///
/// Returns `Ok(vec![])` when the state file does not exist.
/// Returns `Err` when the file exists but cannot be read or parsed,
/// so callers can surface the failure instead of silently masking it.
/// Persistence write path is implemented by GitHub #26.
///
/// Path resolution honours `WH_STATE_PATH` via [`crate::paths::state_path`].
pub fn load_jobs() -> Result<Vec<JobStatus>, String> {
    load_jobs_from(&state_path())
}

/// Load jobs from an explicit state file path (used by tests and future callers).
///
/// This is the implementation behind [`load_jobs`] and is the seam used to test
/// success and malformed-JSON behaviour for the `WH_STATE_PATH` store without
/// mutating process environment (workspace forbids `unsafe-code`).
pub fn load_jobs_from(path: &Path) -> Result<Vec<JobStatus>, String> {
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(format!("failed to read {}: {}", path.display(), e)),
    };
    serde_json::from_str(&data).map_err(|e| format!("failed to parse {}: {}", path.display(), e))
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::load_jobs_from;
    use crate::paths::resolve_state_path;
    use crate::status::{CiClass, JobStatus, ProcessState};

    fn sample_job() -> JobStatus {
        JobStatus {
            job_id: "wh-1".to_owned(),
            owner: "acme".to_owned(),
            repo: "example-org".to_owned(),
            issue_number: Some(1),
            pr_number: None,
            worktree_path: "/tmp/worktrees/acme/example-org/wh-1".to_owned(),
            branch: "feature/status".to_owned(),
            process_state: ProcessState::Running,
            last_error: None,
            ci_class: CiClass::Pending,
        }
    }

    fn unique_path(prefix: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "{prefix}-{}-{}.json",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn wh_state_path_load_success() {
        // Simulate WH_STATE_PATH pointing at a valid watched.json.
        let path = resolve_state_path(Some(unique_path("wh-state-ok").to_str().unwrap()));
        let jobs = vec![sample_job()];
        fs::write(&path, serde_json::to_string(&jobs).unwrap()).unwrap();

        let loaded = load_jobs_from(&path).expect("load success");
        let _ = fs::remove_file(&path);

        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].job_id, "wh-1");
        assert_eq!(loaded[0].owner, "acme");
        assert_eq!(loaded[0].repo, "example-org");
    }

    #[test]
    fn wh_state_path_load_malformed_json() {
        // Simulate WH_STATE_PATH pointing at a corrupt watched.json.
        let path = resolve_state_path(Some(unique_path("wh-state-bad").to_str().unwrap()));
        fs::write(&path, "{not valid json").unwrap();

        let err = load_jobs_from(&path).expect_err("malformed JSON must fail");
        let _ = fs::remove_file(&path);

        assert!(
            err.contains("failed to parse"),
            "unexpected error message: {err}"
        );
    }

    #[test]
    fn load_jobs_from_missing_file_returns_empty() {
        let path = unique_path("wh-state-missing");
        let _ = fs::remove_file(&path);
        let loaded = load_jobs_from(&path).expect("missing is empty");
        assert!(loaded.is_empty());
    }
}
