//! Status and job-query JSON types for Python and agent consumption.

use serde::{Deserialize, Serialize};

use crate::contract::{Response, SCHEMA_VERSION};

/// Lifecycle state of a watched job process.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessState {
    /// Job has been created but not yet started.
    Pending,
    /// Job is actively running.
    Running,
    /// Job finished successfully.
    Completed,
    /// Job terminated with an error.
    Failed,
    /// Job was cancelled by the operator.
    Cancelled,
}

/// CI check-run classification for a job's head commit.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CiClass {
    /// All required checks passed.
    Pass,
    /// At least one required check failed.
    Fail,
    /// Checks are queued or in progress.
    Pending,
    /// CI status could not be determined.
    Unknown,
}

/// Status of a single watched job.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct JobStatus {
    /// Unique job identifier (e.g. `wh-347`).
    pub job_id: String,
    /// Repository owner (e.g. `rmems`).
    pub owner: String,
    /// Repository name (e.g. `worktrees-hives`).
    pub repo: String,
    /// Linked issue number, if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub issue_number: Option<u64>,
    /// Linked pull-request number, if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pr_number: Option<u64>,
    /// Absolute path to the job's isolated worktree.
    pub worktree_path: String,
    /// Current branch checked out in the worktree.
    pub branch: String,
    /// Lifecycle state of the job process.
    pub process_state: ProcessState,
    /// Last error message, if the job is in `Failed` state.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    /// CI classification for the job's head commit.
    pub ci_class: CiClass,
}

/// Payload for `wh status` and `wh jobs` v1 envelope responses.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct JobsData {
    /// List of job statuses (may be empty when no jobs are watched).
    pub jobs: Vec<JobStatus>,
}

/// Build a successful v1 envelope response for the given command and job list.
///
/// The returned value serializes as `{ ok, schema_version, command, data: { jobs }, error }`,
/// matching the shared envelope contract defined in [`crate::contract::Response`].
#[must_use]
pub fn status_response(command: &'static str, jobs: Vec<JobStatus>) -> Response<JobsData> {
    Response::success(command, JobsData { jobs })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_job() -> JobStatus {
        JobStatus {
            job_id: "wh-100".to_owned(),
            owner: "rmems".to_owned(),
            repo: "worktrees-hives".to_owned(),
            issue_number: Some(29),
            pr_number: Some(42),
            worktree_path: "/tmp/worktrees/rmems/worktrees-hives/wh-100".to_owned(),
            branch: "feature/status-json-cli".to_owned(),
            process_state: ProcessState::Running,
            last_error: None,
            ci_class: CiClass::Pending,
        }
    }

    #[test]
    fn job_status_serializes_with_optional_fields_present() {
        let job = JobStatus {
            last_error: Some("task failed: permission denied".to_owned()),
            ..sample_job()
        };
        let json = serde_json::to_string(&job).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(v["job_id"], "wh-100");
        assert_eq!(v["process_state"], "running");
        assert_eq!(v["ci_class"], "pending");
        assert_eq!(v["issue_number"], 29);
        assert_eq!(v["pr_number"], 42);
        assert!(v.get("last_error").is_some());
        assert_eq!(v["last_error"], "task failed: permission denied");
    }

    #[test]
    fn job_status_omits_none_fields() {
        let job = JobStatus {
            issue_number: None,
            pr_number: None,
            last_error: None,
            ..sample_job()
        };
        let json = serde_json::to_string(&job).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert!(v.get("issue_number").is_none());
        assert!(v.get("pr_number").is_none());
        assert!(v.get("last_error").is_none());
    }

    #[test]
    fn status_response_uses_v1_envelope() {
        let response = status_response("cli.status", vec![sample_job()]);
        let json = serde_json::to_string(&response).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(v["schema_version"], SCHEMA_VERSION);
        assert_eq!(v["command"], "cli.status");
        assert!(v["ok"].as_bool().unwrap());
        assert!(v["error"].is_null());
        assert_eq!(v["data"]["jobs"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn empty_response_has_zero_jobs() {
        let response = status_response("cli.jobs", Vec::new());
        assert!(response.data.jobs.is_empty());
        assert!(response.ok);
    }

    #[test]
    fn roundtrip_through_json() {
        let response = status_response("cli.status", vec![sample_job()]);
        let json = serde_json::to_string(&response).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(v["command"], "cli.status");
        assert_eq!(v["data"]["jobs"][0]["job_id"], "wh-100");
        assert_eq!(v["data"]["jobs"][0]["branch"], "feature/status-json-cli");
    }

    #[test]
    fn process_state_variants_serialize_correctly() {
        let cases = [
            (ProcessState::Pending, "\"pending\""),
            (ProcessState::Running, "\"running\""),
            (ProcessState::Completed, "\"completed\""),
            (ProcessState::Failed, "\"failed\""),
            (ProcessState::Cancelled, "\"cancelled\""),
        ];
        for (state, expected) in cases {
            assert_eq!(serde_json::to_string(&state).unwrap(), expected);
        }
    }

    #[test]
    fn ci_class_variants_serialize_correctly() {
        let cases = [
            (CiClass::Pass, "\"pass\""),
            (CiClass::Fail, "\"fail\""),
            (CiClass::Pending, "\"pending\""),
            (CiClass::Unknown, "\"unknown\""),
        ];
        for (class, expected) in cases {
            assert_eq!(serde_json::to_string(&class).unwrap(), expected);
        }
    }
}
