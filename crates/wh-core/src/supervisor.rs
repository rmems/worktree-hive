//! Process supervisor with timeouts, process-group isolation, and max-parallel enforcement.
//!
//! # Platform notes
//!
//! On **Unix**, timeout and kill paths send `SIGKILL` to the entire process group
//! (`kill(-pid, SIGKILL)` after spawning with `process_group(0)`), so descendants
//! started by the child are cleaned up with the supervised process.
//!
//! On **Windows**, only the direct child process is killed via `child.kill()`.
//! There is **no kill-tree**: grandchild processes may outlive the supervisor.
//! Full Windows job-object / kill-tree support is not implemented yet.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use serde::Serialize;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Semaphore;
use tokio::task::JoinHandle;

/// How long to wait for the child to exit after a timeout kill.
const POST_KILL_JOIN_TIMEOUT: Duration = Duration::from_secs(2);

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
}

impl SupervisedOutput {
    /// True when the supervisor failed to spawn the process.
    #[must_use]
    pub fn spawn_failed(&self) -> bool {
        self.exit_code.is_none()
            && !self.timed_out
            && !self.killed
            && self.stderr.starts_with("failed to spawn:")
    }
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

impl Supervisor {
    /// Create a new supervisor with the given max-parallel limit.
    pub fn new(max_parallel: usize) -> Self {
        let permits = max_parallel.max(1);
        Self {
            max_parallel: permits,
            semaphore: Semaphore::new(permits),
            active: AtomicUsize::new(0),
            peak_active: AtomicUsize::new(0),
        }
    }

    /// Returns the configured max-parallel limit.
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

    /// Run a command under supervision.
    ///
    /// - Spawns with process-group isolation where the OS allows.
    /// - Enforces wall-clock timeout; kills the process group on expiry.
    /// - Acquires a permit from the global max-parallel semaphore before spawning.
    pub async fn run(
        &self,
        program: &str,
        args: &[&str],
        timeout: Option<Duration>,
    ) -> SupervisedOutput {
        let _permit = self
            .semaphore
            .acquire()
            .await
            .expect("supervisor semaphore closed");

        let current = self.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak_active.fetch_max(current, Ordering::SeqCst);

        let output = self.run_with_permit(program, args, timeout).await;

        self.active.fetch_sub(1, Ordering::SeqCst);
        output
    }

    async fn run_with_permit(
        &self,
        program: &str,
        args: &[&str],
        timeout: Option<Duration>,
    ) -> SupervisedOutput {
        let mut cmd = Command::new(program);
        cmd.args(args);
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());

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
            let deadline = tokio::time::sleep(timeout);
            tokio::pin!(deadline);

            tokio::select! {
                biased;
                _ = &mut deadline => {
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
                    }
                }
                status = child.wait() => {
                    let stdout = stdout_handle.await.unwrap_or_default();
                    let stderr = stderr_handle.await.unwrap_or_default();
                    match status {
                        Ok(s) => output_to_supervised(s, &stdout, &stderr),
                        Err(e) => SupervisedOutput {
                            exit_code: None,
                            timed_out: false,
                            killed: false,
                            stdout: String::new(),
                            stderr: format!("process error: {e}"),
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
                },
            }
        }
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

    SupervisedOutput {
        exit_code,
        timed_out: false,
        killed,
        stdout: String::from_utf8_lossy(stdout).into_owned(),
        stderr: String::from_utf8_lossy(stderr).into_owned(),
    }
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
    // Windows: only the direct child is killed via `child.kill()` in the caller.
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

    #[tokio::test]
    async fn runs_command_to_completion() {
        let supervisor = Supervisor::new(4);
        let output = supervisor
            .run(shell_program(), &[shell_flag(), "echo hello"], None)
            .await;

        assert_eq!(output.exit_code, Some(0), "stderr={}", output.stderr);
        assert!(!output.timed_out);
        assert!(!output.killed);
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
            .run(shell_program(), &[shell_flag(), script], None)
            .await;

        assert_eq!(output.exit_code, Some(0), "stderr={}", output.stderr);
        assert_eq!(output.stderr.trim(), "err");
    }

    #[tokio::test]
    async fn timeout_kills_process_group() {
        let supervisor = Supervisor::new(4);
        // Use a long-running shell sleep so we do not depend on a `sleep` binary
        // being on PATH (Windows CI may lack it outside Git usr/bin).
        #[cfg(windows)]
        let script = "ping -n 60 127.0.0.1 >NUL";
        #[cfg(not(windows))]
        let script = "sleep 60";
        let output = supervisor
            .run(
                shell_program(),
                &[shell_flag(), script],
                Some(Duration::from_millis(200)),
            )
            .await;

        assert!(output.timed_out, "stderr={}", output.stderr);
        assert!(output.killed);
        assert!(output.exit_code.is_none());
    }

    #[tokio::test]
    async fn propagates_nonzero_exit_code() {
        let supervisor = Supervisor::new(4);
        #[cfg(windows)]
        let script = "exit /B 42";
        #[cfg(not(windows))]
        let script = "exit 42";
        let output = supervisor
            .run(shell_program(), &[shell_flag(), script], None)
            .await;

        assert_eq!(output.exit_code, Some(42), "stderr={}", output.stderr);
        assert!(!output.timed_out);
        assert!(!output.killed);
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
                s.run(shell_program(), &[shell_flag(), script], None).await
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
            )
            .await;

        let json = serde_json::to_string(&output).unwrap();
        assert!(json.contains("\"timed_out\":true"), "{json}");
        assert!(json.contains("\"killed\":true"), "{json}");
        assert!(json.contains("\"exit_code\":null"), "{json}");
    }

    #[tokio::test]
    async fn spawn_failure_is_detectable() {
        let supervisor = Supervisor::new(1);
        let output = supervisor.run("wh-nonexistent-binary-xyz", &[], None).await;
        assert!(output.spawn_failed(), "stderr={}", output.stderr);
        assert!(output.exit_code.is_none());
    }
}
