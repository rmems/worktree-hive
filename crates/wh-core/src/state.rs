//! Durable watched-job state and atomic persistence.
//!
//! The `watched.json` store is implemented by GitHub #26.

use crate::status::JobStatus;

/// Return all currently watched jobs from the persisted store.
///
/// Returns an empty list until the `watched.json` persistence layer from
/// GitHub #26 is wired up.
pub fn load_jobs() -> Vec<JobStatus> {
    Vec::new()
}
