use std::io::{self, Write};
use std::process::ExitCode;

use clap::{Parser, Subcommand};

/// Manage isolated issue-to-PR and PR-babysit jobs.
#[derive(Debug, Parser)]
#[command(name = "wh", version, about, long_about = None)]
struct Cli {
    /// Emit responses as v1 JSON envelopes.
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Show status of all watched jobs.
    Status,
    /// List all watched jobs (alias for status).
    Jobs,
    /// Validate and run a git command under safety policy.
    GitSafe {
        /// Git arguments after the subcommand (e.g., push --force-with-lease origin main).
        #[arg(required = true)]
        args: Vec<String>,
    },

    /// Validate and run a GitHub CLI command under safety policy.
    GhSafe {
        /// gh arguments (e.g., pr create --title "Fix").
        #[arg(required = true)]
        args: Vec<String>,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();

    match run(cli, &mut io::stdout()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(io::stderr(), "wh: {error}");
            match &error {
                wh_core::error::Error::PolicyViolation { .. } => ExitCode::from(2),
                _ => ExitCode::FAILURE,
            }
        }
    }
}

/// Entry point that loads watched state only for status/jobs subcommands.
fn run(cli: Cli, stdout: &mut impl Write) -> wh_core::error::Result<()> {
    match cli.command {
        Some(Command::Status) => {
            run_status(cli.json, "cli.status", wh_core::state::load_jobs(), stdout)
                .map_err(wh_core::error::Error::from)
        }
        Some(Command::Jobs) => {
            run_status(cli.json, "cli.jobs", wh_core::state::load_jobs(), stdout)
                .map_err(wh_core::error::Error::from)
        }
        Some(Command::GitSafe { args }) => run_git_safe(&args, cli.json, stdout),
        Some(Command::GhSafe { args }) => run_gh_safe(&args, cli.json, stdout),
        None => {
            if cli.json {
                serde_json::to_writer(
                    &mut *stdout,
                    &wh_core::contract::Response::bootstrap_success(),
                )
                .map_err(io::Error::other)?;
                stdout.write_all(b"\n")?;
            }
            Ok(())
        }
    }
}

/// Render status/jobs from a preloaded `load_jobs` result (testable without env mutation).
fn run_status(
    json: bool,
    command_name: &'static str,
    jobs_result: Result<Vec<wh_core::status::JobStatus>, String>,
    stdout: &mut impl Write,
) -> io::Result<()> {
    match jobs_result {
        Ok(jobs) => run_with_jobs(json, command_name, jobs, stdout),
        Err(e) => {
            if json {
                // JSON path: write ok:false envelope, then exit non-zero.
                // Consumers should parse stdout even on CalledProcessError (see docs/status-schema.md).
                let response = wh_core::status::status_error(command_name, e.clone());
                serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
                stdout.write_all(b"\n")?;
            }
            // Human and JSON: never pretend the empty set is healthy on load failure.
            Err(io::Error::other(e))
        }
    }
}

/// Render status/jobs output. Separated from `run` for testability.
fn run_with_jobs(
    json: bool,
    command_name: &'static str,
    jobs: Vec<wh_core::status::JobStatus>,
    stdout: &mut impl Write,
) -> io::Result<()> {
    if json {
        let response = wh_core::status::status_response(command_name, jobs);
        serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    } else if jobs.is_empty() {
        stdout.write_all(b"No watched jobs.\n")?;
    } else {
        for job in &jobs {
            writeln!(
                stdout,
                "{} [{}] {}/{} branch={} ci={}",
                job.job_id, job.process_state, job.owner, job.repo, job.branch, job.ci_class
            )?;
        }
    }
    Ok(())
}

fn run_git_safe(
    args: &[String],
    json: bool,
    stdout: &mut impl Write,
) -> wh_core::error::Result<()> {
    let cmd = wh_core::git_safe::SafeGitCommand::new(args)?;

    if json {
        let response = wh_core::contract::Response {
            ok: true,
            schema_version: wh_core::contract::SCHEMA_VERSION,
            command: "git.safe",
            data: wh_core::contract::EmptyData::default(),
            error: None,
        };
        serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    } else {
        writeln!(stdout, "validated: git {}", cmd.args().join(" "))?;
    }

    Ok(())
}

fn run_gh_safe(args: &[String], json: bool, stdout: &mut impl Write) -> wh_core::error::Result<()> {
    let cmd = wh_core::git_safe::SafeGhCommand::new(args)?;

    if json {
        let response = wh_core::contract::Response {
            ok: true,
            schema_version: wh_core::contract::SCHEMA_VERSION,
            command: "gh.safe",
            data: wh_core::contract::EmptyData::default(),
            error: None,
        };
        serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    } else {
        writeln!(stdout, "validated: gh {}", cmd.args().join(" "))?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;
    use std::str;
    use wh_core::status::{CiClass, JobStatus, ProcessState};

    use super::{Cli, run, run_status, run_with_jobs};

    fn sample_job() -> JobStatus {
        JobStatus {
            job_id: "wh-1".to_owned(),
            owner: "acme".to_owned(),
            repo: "example-org".to_owned(),
            issue_number: Some(29),
            pr_number: None,
            worktree_path: "/tmp/wt/wh-1".to_owned(),
            branch: "feature/foo".to_owned(),
            process_state: ProcessState::Running,
            last_error: None,
            ci_class: CiClass::Pending,
        }
    }

    #[test]
    fn command_definition_is_valid() {
        let command = Cli::command();

        assert_eq!(command.get_version(), Some(wh_core::VERSION));
        command.debug_assert();
    }

    #[test]
    fn json_mode_writes_v1_envelope_to_stdout() {
        let cli = Cli {
            json: true,
            command: None,
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        assert_eq!(
            str::from_utf8(&stdout).unwrap(),
            "{\"ok\":true,\"schema_version\":1,\"command\":\"cli.bootstrap\",\"data\":{},\"error\":null}\n"
        );
    }

    #[test]
    fn default_mode_keeps_stdout_empty() {
        let cli = Cli {
            json: false,
            command: None,
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        assert!(stdout.is_empty());
    }

    #[test]
    fn status_json_emits_v1_envelope_with_jobs_in_data() {
        let mut stdout = Vec::new();

        run_with_jobs(true, "cli.status", vec![], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        assert_eq!(v.get("schema_version").expect("missing schema_version"), 1);
        assert_eq!(v.get("command").expect("missing command"), "cli.status");
        assert!(v.get("ok").expect("missing ok").as_bool().unwrap());
        assert!(
            v.get("error").expect("missing error").is_null(),
            "error must be explicitly null, not absent"
        );
        let data = v.get("data").expect("missing data");
        let jobs = data
            .get("jobs")
            .expect("missing data.jobs")
            .as_array()
            .expect("data.jobs must be an array");
        assert!(jobs.is_empty());
        assert!(v.get("jobs").is_none(), "jobs must be nested under data");
    }

    #[test]
    fn status_json_includes_injected_jobs() {
        let mut stdout = Vec::new();

        run_with_jobs(true, "cli.status", vec![sample_job()], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        let data = v.get("data").expect("missing data");
        let jobs = data
            .get("jobs")
            .expect("missing data.jobs")
            .as_array()
            .expect("data.jobs must be an array");
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].get("job_id").expect("missing job_id"), "wh-1");
        assert_eq!(
            jobs[0].get("process_state").expect("missing process_state"),
            "running"
        );
    }

    #[test]
    fn jobs_json_emits_v1_envelope_with_jobs_in_data() {
        let mut stdout = Vec::new();

        run_with_jobs(true, "cli.jobs", vec![], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        assert_eq!(v.get("schema_version").expect("missing schema_version"), 1);
        assert_eq!(v.get("command").expect("missing command"), "cli.jobs");
        assert!(v.get("ok").expect("missing ok").as_bool().unwrap());
        assert!(
            v.get("error").expect("missing error").is_null(),
            "error must be explicitly null, not absent"
        );
        let data = v.get("data").expect("missing data");
        let jobs = data
            .get("jobs")
            .expect("missing data.jobs")
            .as_array()
            .expect("data.jobs must be an array");
        assert!(jobs.is_empty());
        assert!(v.get("jobs").is_none(), "jobs must be nested under data");
    }

    #[test]
    fn status_without_json_prints_human_readable() {
        let mut stdout = Vec::new();

        run_with_jobs(false, "cli.status", vec![], &mut stdout).unwrap();

        assert_eq!(str::from_utf8(&stdout).unwrap(), "No watched jobs.\n");
    }

    #[test]
    fn jobs_without_json_prints_human_readable() {
        let mut stdout = Vec::new();

        run_with_jobs(false, "cli.jobs", vec![], &mut stdout).unwrap();

        assert_eq!(str::from_utf8(&stdout).unwrap(), "No watched jobs.\n");
    }

    #[test]
    fn status_without_json_lists_jobs_when_present() {
        let mut stdout = Vec::new();

        run_with_jobs(false, "cli.status", vec![sample_job()], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("wh-1"));
        assert!(output.contains("running"));
        assert!(output.contains("feature/foo"));
    }

    #[test]
    fn status_json_load_error_emits_failure_envelope() {
        let mut stdout = Vec::new();
        let err = run_status(
            true,
            "cli.status",
            Err("failed to parse watched.json: expected value".to_owned()),
            &mut stdout,
        )
        .unwrap_err();
        assert!(err.to_string().contains("failed to parse"));

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();
        assert_eq!(v.get("ok").expect("missing ok"), false);
        assert_eq!(
            v.get("error")
                .expect("missing error")
                .get("code")
                .expect("missing code"),
            "STATE_LOAD_FAILED"
        );
        assert!(
            v.get("data")
                .expect("missing data")
                .get("jobs")
                .expect("missing jobs")
                .as_array()
                .expect("jobs array")
                .is_empty()
        );
    }

    #[test]
    fn status_human_load_error_does_not_print_healthy_empty() {
        let mut stdout = Vec::new();
        let err = run_status(
            false,
            "cli.status",
            Err("failed to read watched.json: permission denied".to_owned()),
            &mut stdout,
        )
        .unwrap_err();
        assert!(err.to_string().contains("permission denied"));
        // Must not look like a healthy empty watch list.
        assert!(
            !str::from_utf8(&stdout).unwrap().contains("No watched jobs"),
            "human load error must not print healthy empty summary"
        );
        assert!(
            stdout.is_empty(),
            "human load error should not write a success summary to stdout"
        );
    }

    #[test]
    fn git_safe_valid_command() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GitSafe {
                args: vec!["status".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("validated: git status"));
    }

    #[test]
    fn git_safe_json_mode() {
        let cli = Cli {
            json: true,
            command: Some(super::Command::GitSafe {
                args: vec!["push".to_owned(), "--force-with-lease".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("\"ok\":true"));
        assert!(output.contains("\"command\":\"git.safe\""));
    }

    #[test]
    fn git_safe_blocks_merge() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GitSafe {
                args: vec!["merge".to_owned(), "feature".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn git_safe_blocks_bare_force() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GitSafe {
                args: vec!["push".to_owned(), "--force".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn gh_safe_pr_merge_blocked() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GhSafe {
                args: vec!["pr".to_owned(), "merge".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn gh_safe_pr_create_allowed() {
        let cli = Cli {
            json: true,
            command: Some(super::Command::GhSafe {
                args: vec![
                    "pr".to_owned(),
                    "create".to_owned(),
                    "--title".to_owned(),
                    "test".to_owned(),
                ],
            }),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("\"ok\":true"));
        assert!(output.contains("\"command\":\"gh.safe\""));
    }
}
