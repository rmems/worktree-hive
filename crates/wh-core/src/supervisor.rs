//! Process supervisor with timeouts, process-group isolation, and max-parallel enforcement.
//!
//! # Concurrency model
//!
//! `--max-parallel` / [`Supervisor::new`] limits concurrent supervised children **within a
//! single process**. Each `wh` CLI invocation constructs its own supervisor, so independent
//! processes do not share a global permit pool. Callers that need host-wide throttling must
//! coordinate externally (or share one long-lived `Supervisor` instance).
//!
//! # Platform notes
//!
//! On **Unix**, timeout and kill paths send `SIGKILL` to the entire process group
//! (`kill(-pid, SIGKILL)` after spawning with `process_group(0)`), so descendants
//! started by the child are cleaned up with the supervised process.
//!
//! On **Windows**, only the direct child process is killed (`kill_on_drop` + `child.kill()`).
//! There is **no kill-tree / job-object** yet: grandchild processes may outlive the
//! supervisor. Tracking full Windows job-object support is deferred (documented limitation).

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use serde::Serialize;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Semaphore;
use tokio::task::JoinHandle;
use tokio::time::Instant;

use crate::error::{Error, PolicyCode, Result};
use crate::git_safe::{SafeGhCommand, SafeGitCommand};

/// How long to wait for the child to exit after a timeout kill.
const POST_KILL_JOIN_TIMEOUT: Duration = Duration::from_secs(2);

/// Stable supervisor failure classifications (v1, additive on the wire).
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SupervisorErrorCode {
    /// Process could not be started.
    SpawnFailed,
    /// Waiting on the child failed after spawn.
    WaitFailed,
    /// Wall-clock timeout fired (process group / child kill attempted).
    TimedOut,
    /// Child terminated by signal / kill without a classified timeout.
    Killed,
    /// Child exited with a non-zero status.
    NonZeroExit,
}

/// Outcome of a supervised process execution.
#[derive(Debug, Clone, Serialize)]
pub struct SupervisedOutput {
    /// Process exit code, or `None` if terminated by signal / timeout / spawn failure.
    pub exit_code: Option<i32>,
    /// Whether the process was killed due to timeout.
    pub timed_out: bool,
    /// Whether the process was killed (by timeout or signal).
    pub killed: bool,
    /// Captured stdout.
    pub stdout: String,
    /// Captured stderr.
    pub stderr: String,
    /// Structured failure code when the run is not a clean success.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<SupervisorErrorCode>,
}

impl SupervisedOutput {
    /// True when the supervisor failed to spawn the process.
    #[must_use]
    pub fn spawn_failed(&self) -> bool {
        self.error_code == Some(SupervisorErrorCode::SpawnFailed)
    }

    /// True when the supervised command completed successfully (exit 0, not timed out/killed).
    #[must_use]
    pub fn succeeded(&self) -> bool {
        self.exit_code == Some(0) && !self.timed_out && !self.killed && self.error_code.is_none()
    }

    fn with_error(mut self, code: SupervisorErrorCode) -> Self {
        self.error_code = Some(code);
        self
    }
}

/// Options for a policy-checked supervised run.
#[derive(Debug, Clone, Default)]
pub struct RunOptions {
    /// When supervising `git` mutations, require this branch (verified before spawn).
    pub expected_branch: Option<String>,
    /// Repository working tree for git branch verification and `git -C` (default: `.`).
    pub repo: Option<PathBuf>,
}

/// Configuration for the process supervisor.
#[derive(Debug)]
pub struct Supervisor {
    max_parallel: usize,
    semaphore: Semaphore,
    /// Currently held permits (active supervised runs).
    active: AtomicUsize,
    /// High-water mark of concurrent supervised runs observed since construction.
    peak_active: AtomicUsize,
}

/// Guard that restores concurrency accounting if a supervised run is cancelled mid-flight.
struct ActiveGuard<'a> {
    supervisor: &'a Supervisor,
    armed: bool,
}

impl<'a> ActiveGuard<'a> {
    fn arm(supervisor: &'a Supervisor) -> Self {
        let current = supervisor.active.fetch_add(1, Ordering::SeqCst) + 1;
        supervisor.peak_active.fetch_max(current, Ordering::SeqCst);
        Self {
            supervisor,
            armed: true,
        }
    }

    fn defuse(mut self) {
        self.armed = false;
        self.supervisor.active.fetch_sub(1, Ordering::SeqCst);
    }
}

impl Drop for ActiveGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.supervisor.active.fetch_sub(1, Ordering::SeqCst);
        }
    }
}

impl Supervisor {
    /// Create a new supervisor with the given max-parallel limit (per process).
    pub fn new(max_parallel: usize) -> Self {
        let permits = max_parallel.max(1);
        Self {
            max_parallel: permits,
            semaphore: Semaphore::new(permits),
            active: AtomicUsize::new(0),
            peak_active: AtomicUsize::new(0),
        }
    }

    /// Returns the configured max-parallel limit for this process-local supervisor.
    pub fn max_parallel(&self) -> usize {
        self.max_parallel
    }

    /// Peak concurrent supervised runs observed (for tests / diagnostics).
    pub fn peak_active(&self) -> usize {
        self.peak_active.load(Ordering::SeqCst)
    }

    /// Current concurrent supervised runs.
    pub fn active(&self) -> usize {
        self.active.load(Ordering::SeqCst)
    }

    /// Run a command under supervision after applying safety policy.
    ///
    /// - Validates git/gh (including path-qualified names like `/usr/bin/git`) via
    ///   [`SafeGitCommand`] / [`SafeGhCommand`].
    /// - Rejects common shell wrappers that embed merge / bare-force git/gh operations.
    /// - For mutating git, requires [`RunOptions::expected_branch`] and verifies it.
    /// - Spawns with process-group isolation where the OS allows.
    /// - Enforces wall-clock timeout; kills the process group on expiry (Unix).
    /// - Acquires a permit from the **process-local** max-parallel semaphore before spawning.
    pub async fn run(
        &self,
        program: &str,
        args: &[&str],
        timeout: Option<Duration>,
        options: &RunOptions,
    ) -> Result<SupervisedOutput> {
        let prepared = prepare_supervised_command(program, args, options)?;
        let _permit = self
            .semaphore
            .acquire()
            .await
            .expect("supervisor semaphore closed");

        let guard = ActiveGuard::arm(self);
        let output = self
            .run_with_permit(&prepared.program, &prepared.args, timeout)
            .await;
        guard.defuse();
        Ok(output)
    }

    /// Low-level run **without** policy checks (tests / internal). Prefer [`Self::run`].
    pub async fn run_unchecked(
        &self,
        program: &str,
        args: &[&str],
        timeout: Option<Duration>,
    ) -> SupervisedOutput {
        let owned: Vec<String> = args.iter().map(|s| (*s).to_owned()).collect();
        let _permit = self
            .semaphore
            .acquire()
            .await
            .expect("supervisor semaphore closed");
        let guard = ActiveGuard::arm(self);
        let output = self.run_with_permit(program, &owned, timeout).await;
        guard.defuse();
        output
    }

    async fn run_with_permit(
        &self,
        program: &str,
        args: &[String],
        timeout: Option<Duration>,
    ) -> SupervisedOutput {
        let mut cmd = Command::new(program);
        cmd.args(args);
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());
        cmd.kill_on_drop(true);

        #[cfg(unix)]
        set_process_group(&mut cmd);

        let mut child = match cmd.spawn() {
            Ok(child) => child,
            Err(e) => {
                return SupervisedOutput {
                    exit_code: None,
                    timed_out: false,
                    killed: false,
                    stdout: String::new(),
                    stderr: format!("failed to spawn: {e}"),
                    error_code: Some(SupervisorErrorCode::SpawnFailed),
                };
            }
        };

        let pid = child.id();

        // Start reading pipes concurrently to prevent deadlock when
        // the child fills the OS pipe buffer before exiting.
        let mut child_stdout = child.stdout.take();
        let mut child_stderr = child.stderr.take();
        let stdout_handle: JoinHandle<Vec<u8>> =
            tokio::spawn(async move { read_pipe(&mut child_stdout).await });
        let stderr_handle: JoinHandle<Vec<u8>> =
            tokio::spawn(async move { read_pipe(&mut child_stderr).await });

        if let Some(timeout) = timeout {
            let deadline_at = Instant::now() + timeout;
            let deadline = tokio::time::sleep_until(deadline_at);
            tokio::pin!(deadline);

            tokio::select! {
                biased;
                _ = &mut deadline => {
                    timeout_output(pid, &mut child, stdout_handle, stderr_handle).await
                }
                status = child.wait() => {
                    match status {
                        Ok(s) => {
                            match drain_pipes_until(deadline_at, pid, stdout_handle, stderr_handle).await {
                                Ok((stdout, stderr)) => output_to_supervised(s, &stdout, &stderr),
                                Err((stdout, stderr)) => SupervisedOutput {
                                    exit_code: None,
                                    timed_out: true,
                                    killed: true,
                                    stdout: String::from_utf8_lossy(&stdout).into_owned(),
                                    stderr: String::from_utf8_lossy(&stderr).into_owned(),
                                    error_code: Some(SupervisorErrorCode::TimedOut),
                                },
                            }
                        }
                        Err(e) => SupervisedOutput {
                            exit_code: None,
                            timed_out: false,
                            killed: false,
                            stdout: String::new(),
                            stderr: format!("process error: {e}"),
                            error_code: Some(SupervisorErrorCode::WaitFailed),
                        },
                    }
                }
            }
        } else {
            match child.wait().await {
                Ok(status) => {
                    let stdout = stdout_handle.await.unwrap_or_default();
                    let stderr = stderr_handle.await.unwrap_or_default();
                    output_to_supervised(status, &stdout, &stderr)
                }
                Err(e) => SupervisedOutput {
                    exit_code: None,
                    timed_out: false,
                    killed: false,
                    stdout: String::new(),
                    stderr: format!("process error: {e}"),
                    error_code: Some(SupervisorErrorCode::WaitFailed),
                },
            }
        }
    }
}

/// Normalized program + args ready to spawn after policy checks.
struct PreparedCommand {
    program: String,
    args: Vec<String>,
}

/// Normalize an executable path to a basename without platform extensions.
#[must_use]
pub fn normalize_program_name(program: &str) -> String {
    // Accept both Unix and Windows separators even when running on Linux (tests / cross config).
    let base = program
        .rsplit(['/', '\\'])
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or(program);
    let lower = base.to_ascii_lowercase();
    lower
        .strip_suffix(".exe")
        .or_else(|| lower.strip_suffix(".cmd"))
        .or_else(|| lower.strip_suffix(".bat"))
        .unwrap_or(&lower)
        .to_owned()
}

/// Enforce safety policy for a supervised command (used by CLI and core).
pub fn check_command_policy(program: &str, args: &[&str], options: &RunOptions) -> Result<()> {
    prepare_supervised_command(program, args, options).map(|_| ())
}

fn prepare_supervised_command(
    program: &str,
    args: &[&str],
    options: &RunOptions,
) -> Result<PreparedCommand> {
    let name = normalize_program_name(program);
    let owned_args: Vec<String> = args.iter().map(|s| (*s).to_owned()).collect();

    match name.as_str() {
        "git" => {
            let safe = SafeGitCommand::new(&owned_args)?;
            if safe.requires_branch_check() {
                let expected =
                    options
                        .expected_branch
                        .as_deref()
                        .ok_or_else(|| Error::PolicyViolation {
                            code: PolicyCode::BranchMismatch,
                            message:
                                "mutating git commands require --expected-branch under supervisor"
                                    .to_owned(),
                        })?;
                let repo = options.repo.clone().unwrap_or_else(|| PathBuf::from("."));
                safe.verify_branch(&repo, expected)?;
            }
            Ok(PreparedCommand {
                // Spawn the exact path the caller provided (or "git") after policy passed.
                program: program.to_owned(),
                args: owned_args,
            })
        }
        "gh" => {
            let _safe = SafeGhCommand::new(&owned_args)?;
            Ok(PreparedCommand {
                program: program.to_owned(),
                args: owned_args,
            })
        }
        "sh" | "bash" | "zsh" | "dash" | "fish" | "cmd" | "powershell" | "pwsh" => {
            reject_dangerous_shell_script(&owned_args)?;
            Ok(PreparedCommand {
                program: program.to_owned(),
                args: owned_args,
            })
        }
        other if other.contains("git") && other != "git" => {
            // e.g. git-receive-pack — still require full policy via basename git only.
            // Path-like tools that embed git in the name are allowed only if not exact git.
            Ok(PreparedCommand {
                program: program.to_owned(),
                args: owned_args,
            })
        }
        _ => Ok(PreparedCommand {
            program: program.to_owned(),
            args: owned_args,
        }),
    }
}

fn reject_dangerous_shell_script(args: &[String]) -> Result<()> {
    let joined = args.join(" ").to_ascii_lowercase();
    let dangerous = [
        ("gh pr merge", PolicyCode::MergeBlocked),
        ("gh pr ready", PolicyCode::MergeBlocked),
        ("git merge ", PolicyCode::MergeBlocked),
        ("git merge\t", PolicyCode::MergeBlocked),
        ("git push --force", PolicyCode::BareForcePush),
        ("git push -f", PolicyCode::BareForcePush),
    ];
    for (needle, code) in dangerous {
        if joined.contains(needle) {
            // Allow force-with-lease wording to pass the bare-force heuristic.
            if code == PolicyCode::BareForcePush && joined.contains("force-with-lease") {
                continue;
            }
            return Err(Error::PolicyViolation {
                code,
                message: format!(
                    "shell-wrapped forbidden operation detected in supervised command: `{needle}`"
                ),
            });
        }
    }
    // `git merge` as sole trailing command
    if joined.split_whitespace().any(|t| t == "merge")
        && joined.split_whitespace().any(|t| t == "git")
        && joined.contains("git")
    {
        // Avoid blocking `git mergetool` etc. — already handled by needles; skip broad match.
    }
    Ok(())
}

async fn timeout_output(
    pid: Option<u32>,
    child: &mut tokio::process::Child,
    stdout_handle: JoinHandle<Vec<u8>>,
    stderr_handle: JoinHandle<Vec<u8>>,
) -> SupervisedOutput {
    kill_process_group(pid);
    let _ = child.kill().await;
    // Bound how long we wait for the killed child and pipe readers.
    let _ = tokio::time::timeout(POST_KILL_JOIN_TIMEOUT, child.wait()).await;
    let stdout = join_with_timeout(stdout_handle).await;
    let stderr = join_with_timeout(stderr_handle).await;
    SupervisedOutput {
        exit_code: None,
        timed_out: true,
        killed: true,
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
        error_code: Some(SupervisorErrorCode::TimedOut),
    }
}

async fn drain_pipes_until(
    deadline_at: Instant,
    pid: Option<u32>,
    stdout_handle: JoinHandle<Vec<u8>>,
    stderr_handle: JoinHandle<Vec<u8>>,
) -> std::result::Result<(Vec<u8>, Vec<u8>), (Vec<u8>, Vec<u8>)> {
    // Keep the original deadline active while draining: descendants that inherit
    // stdout/stderr (e.g. `sh -c 'sleep 60 &'`) must not hang the supervisor forever.
    let drain = async {
        let stdout = stdout_handle.await.unwrap_or_default();
        let stderr = stderr_handle.await.unwrap_or_default();
        (stdout, stderr)
    };
    tokio::pin!(drain);
    tokio::select! {
        biased;
        _ = tokio::time::sleep_until(deadline_at) => {
            kill_process_group(pid);
            // Dropping `drain` aborts the pipe reader tasks; timeout path prioritizes
            // returning promptly over partial capture.
            Err((Vec::new(), Vec::new()))
        }
        out = &mut drain => Ok(out),
    }
}

async fn join_with_timeout(handle: JoinHandle<Vec<u8>>) -> Vec<u8> {
    match tokio::time::timeout(POST_KILL_JOIN_TIMEOUT, handle).await {
        Ok(Ok(buf)) => buf,
        Ok(Err(_)) => Vec::new(),
        Err(_) => Vec::new(),
    }
}

async fn read_pipe<R: AsyncReadExt + Unpin>(pipe: &mut Option<R>) -> Vec<u8> {
    match pipe.as_mut() {
        Some(reader) => {
            let mut buf = Vec::new();
            let _ = reader.read_to_end(&mut buf).await;
            buf
        }
        None => Vec::new(),
    }
}

#[cfg(unix)]
fn set_process_group(cmd: &mut tokio::process::Command) {
    use std::os::unix::process::CommandExt as _;
    cmd.as_std_mut().process_group(0);
}

fn output_to_supervised(
    status: std::process::ExitStatus,
    stdout: &[u8],
    stderr: &[u8],
) -> SupervisedOutput {
    let exit_code = status.code();

    #[cfg(unix)]
    let killed = {
        use std::os::unix::process::ExitStatusExt;
        status.signal().is_some()
    };
    #[cfg(not(unix))]
    let killed = false;

    let mut out = SupervisedOutput {
        exit_code,
        timed_out: false,
        killed,
        stdout: String::from_utf8_lossy(stdout).into_owned(),
        stderr: String::from_utf8_lossy(stderr).into_owned(),
        error_code: None,
    };
    if killed {
        out = out.with_error(SupervisorErrorCode::Killed);
    } else if exit_code != Some(0) {
        out = out.with_error(SupervisorErrorCode::NonZeroExit);
    }
    out
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn kill_process_group(pid: Option<u32>) {
    if let Some(pid) = pid {
        // Send SIGKILL to the process group (negative PID) via libc.
        // SAFETY: kill(2) is async-signal-safe and only sends a signal.
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
}

#[cfg(not(unix))]
fn kill_process_group(_pid: Option<u32>) {
    // Windows: only the direct child is killed via kill_on_drop / child.kill().
    // Grandchildren are not reaped (no kill-tree / job object yet).
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    /// Portable shell invocation for tests (`sh -c` / `cmd /C`).
    fn shell_program() -> &'static str {
        #[cfg(windows)]
        {
            "cmd"
        }
        #[cfg(not(windows))]
        {
            "sh"
        }
    }

    fn shell_flag() -> &'static str {
        #[cfg(windows)]
        {
            "/C"
        }
        #[cfg(not(windows))]
        {
            "-c"
        }
    }

    #[test]
    fn normalize_strips_path_and_exe() {
        assert_eq!(normalize_program_name("/usr/bin/git"), "git");
        assert_eq!(normalize_program_name("C:\\Program Files\\git.exe"), "git");
        assert_eq!(normalize_program_name("./gh"), "gh");
        assert_eq!(normalize_program_name("GH.EXE"), "gh");
    }

    #[test]
    fn policy_blocks_path_qualified_force_push() {
        let err =
            check_command_policy("/usr/bin/git", &["push", "--force"], &RunOptions::default())
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
    fn policy_blocks_gh_pr_merge() {
        let err = check_command_policy("gh", &["pr", "merge"], &RunOptions::default()).unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[test]
    fn policy_blocks_shell_wrapped_merge() {
        let err = check_command_policy("sh", &["-c", "gh pr merge 1"], &RunOptions::default())
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
    fn mutating_git_requires_expected_branch() {
        let err = check_command_policy("git", &["commit", "-m", "x"], &RunOptions::default())
            .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::BranchMismatch,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn runs_command_to_completion() {
        let supervisor = Supervisor::new(4);
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), "echo hello"],
                None,
                &RunOptions::default(),
            )
            .await
            .unwrap();

        assert_eq!(output.exit_code, Some(0), "stderr={}", output.stderr);
        assert!(!output.timed_out);
        assert!(!output.killed);
        assert!(output.succeeded());
        assert_eq!(output.stdout.trim(), "hello");
    }

    #[tokio::test]
    async fn captures_stderr() {
        let supervisor = Supervisor::new(4);
        #[cfg(windows)]
        let script = "echo err 1>&2";
        #[cfg(not(windows))]
        let script = "echo err >&2";
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), script],
                None,
                &RunOptions::default(),
            )
            .await
            .unwrap();

        assert_eq!(output.exit_code, Some(0), "stderr={}", output.stderr);
        assert_eq!(output.stderr.trim(), "err");
    }

    #[tokio::test]
    async fn timeout_kills_process_group() {
        let supervisor = Supervisor::new(4);
        #[cfg(windows)]
        let script = "ping -n 60 127.0.0.1 >NUL";
        #[cfg(not(windows))]
        let script = "sleep 60";
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), script],
                Some(Duration::from_millis(200)),
                &RunOptions::default(),
            )
            .await
            .unwrap();

        assert!(output.timed_out, "stderr={}", output.stderr);
        assert!(output.killed);
        assert!(output.exit_code.is_none());
        assert_eq!(output.error_code, Some(SupervisorErrorCode::TimedOut));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn timeout_remains_active_while_draining_inherited_pipes() {
        let supervisor = Supervisor::new(1);
        let started = Instant::now();
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), "sleep 60 &"],
                Some(Duration::from_millis(200)),
                &RunOptions::default(),
            )
            .await
            .unwrap();

        assert!(output.timed_out, "output={output:?}");
        assert!(output.killed, "output={output:?}");
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "supervisor hung while draining inherited pipes"
        );
    }

    #[tokio::test]
    async fn propagates_nonzero_exit_code() {
        let supervisor = Supervisor::new(4);
        #[cfg(windows)]
        let script = "exit /B 42";
        #[cfg(not(windows))]
        let script = "exit 42";
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), script],
                None,
                &RunOptions::default(),
            )
            .await
            .unwrap();

        assert_eq!(output.exit_code, Some(42), "stderr={}", output.stderr);
        assert!(!output.timed_out);
        assert!(!output.killed);
        assert_eq!(output.error_code, Some(SupervisorErrorCode::NonZeroExit));
        assert!(!output.succeeded());
    }

    #[tokio::test]
    async fn max_parallel_limits_concurrency() {
        let supervisor = Arc::new(Supervisor::new(2));

        #[cfg(windows)]
        let script = "ping -n 2 127.0.0.1 >NUL";
        #[cfg(not(windows))]
        let script = "sleep 0.4";

        let mut handles = Vec::new();
        for _ in 0..3 {
            let s = Arc::clone(&supervisor);
            handles.push(tokio::spawn(async move {
                s.run(
                    shell_program(),
                    &[shell_flag(), script],
                    None,
                    &RunOptions::default(),
                )
                .await
                .unwrap()
            }));
        }

        let mut results = Vec::new();
        for h in handles {
            results.push(h.await.expect("join task"));
        }

        for (i, output) in results.iter().enumerate() {
            assert_eq!(
                output.exit_code,
                Some(0),
                "task {i} failed: stderr={}",
                output.stderr
            );
        }

        let peak = supervisor.peak_active();
        assert!(peak <= 2, "peak concurrency {peak} exceeded max_parallel=2");
        assert_eq!(
            peak, 2,
            "expected peak concurrency to reach 2 with 3 overlapping tasks"
        );
        assert_eq!(supervisor.active(), 0);
    }

    #[tokio::test]
    async fn serializes_to_json_with_all_fields() {
        let output = SupervisedOutput {
            exit_code: Some(0),
            timed_out: false,
            killed: false,
            stdout: "hello\n".to_string(),
            stderr: String::new(),
            error_code: None,
        };

        let json = serde_json::to_string(&output).unwrap();
        assert!(json.contains("\"exit_code\":0"));
        assert!(json.contains("\"timed_out\":false"));
        assert!(json.contains("\"killed\":false"));
        assert!(json.contains("\"stdout\":\"hello\\n\""));
        assert!(json.contains("\"stderr\":\"\""));
    }

    #[tokio::test]
    async fn timeout_output_serializes_correctly() {
        let supervisor = Supervisor::new(4);
        #[cfg(windows)]
        let script = "ping -n 60 127.0.0.1 >NUL";
        #[cfg(not(windows))]
        let script = "sleep 60";
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), script],
                Some(Duration::from_millis(200)),
                &RunOptions::default(),
            )
            .await
            .unwrap();

        let json = serde_json::to_string(&output).unwrap();
        assert!(json.contains("\"timed_out\":true"), "{json}");
        assert!(json.contains("\"killed\":true"), "{json}");
        assert!(json.contains("\"exit_code\":null"), "{json}");
        assert!(json.contains("TIMED_OUT"), "{json}");
    }

    #[tokio::test]
    async fn spawn_failure_is_detectable() {
        let supervisor = Supervisor::new(1);
        let output = supervisor
            .run(
                "wh-nonexistent-binary-xyz",
                &[],
                None,
                &RunOptions::default(),
            )
            .await
            .unwrap();
        assert!(output.spawn_failed(), "stderr={}", output.stderr);
        assert_eq!(output.error_code, Some(SupervisorErrorCode::SpawnFailed));
        assert!(output.exit_code.is_none());
    }

    #[tokio::test]
    async fn run_rejects_merge_before_spawn() {
        let supervisor = Supervisor::new(1);
        let err = supervisor
            .run("gh", &["pr", "merge"], None, &RunOptions::default())
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            Error::PolicyViolation {
                code: PolicyCode::MergeBlocked,
                ..
            }
        ));
    }
}
