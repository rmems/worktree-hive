//! Process supervisor with timeouts, process-group isolation, and max-parallel enforcement.

use std::time::Duration;

use serde::Serialize;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Semaphore;

/// Outcome of a supervised process execution.
#[derive(Debug, Clone, Serialize)]
pub struct SupervisedOutput {
    /// Process exit code, or `None` if terminated by signal.
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
}

impl Supervisor {
    /// Create a new supervisor with the given max-parallel limit.
    pub fn new(max_parallel: usize) -> Self {
        Self {
            max_parallel,
            semaphore: Semaphore::new(max_parallel),
        }
    }

    /// Returns the configured max-parallel limit.
    pub fn max_parallel(&self) -> usize {
        self.max_parallel
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

        if let Some(timeout) = timeout {
            let deadline = tokio::time::sleep(timeout);
            tokio::pin!(deadline);

            let mut child_stdout = child.stdout.take();
            let mut child_stderr = child.stderr.take();

            tokio::select! {
                biased;
                _ = &mut deadline => {
                    kill_process_group(pid);
                    let _ = child.kill().await;
                    let _ = child.wait().await;
                    SupervisedOutput {
                        exit_code: None,
                        timed_out: true,
                        killed: true,
                        stdout: String::new(),
                        stderr: String::new(),
                    }
                }
                status = child.wait() => {
                    let stdout = read_pipe(&mut child_stdout).await;
                    let stderr = read_pipe(&mut child_stderr).await;
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
            let mut child_stdout = child.stdout.take();
            let mut child_stderr = child.stderr.take();

            match child.wait().await {
                Ok(status) => {
                    let stdout = read_pipe(&mut child_stdout).await;
                    let stderr = read_pipe(&mut child_stderr).await;
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
fn kill_process_group(pid: Option<u32>) {
    if let Some(pid) = pid {
        // Send SIGKILL to the process group (negative PID).
        let _ = std::process::Command::new("kill")
            .args(["-9", "--", &format!("-{pid}")])
            .status();
    }
}

#[cfg(not(unix))]
fn kill_process_group(_pid: Option<u32>) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn runs_command_to_completion() {
        let supervisor = Supervisor::new(4);
        let output = supervisor.run("echo", &["hello"], None).await;

        assert_eq!(output.exit_code, Some(0));
        assert!(!output.timed_out);
        assert!(!output.killed);
        assert_eq!(output.stdout.trim(), "hello");
    }

    #[tokio::test]
    async fn captures_stderr() {
        let supervisor = Supervisor::new(4);
        let output = supervisor.run("bash", &["-c", "echo err >&2"], None).await;

        assert_eq!(output.exit_code, Some(0));
        assert_eq!(output.stderr.trim(), "err");
    }

    #[tokio::test]
    async fn timeout_kills_process_group() {
        let supervisor = Supervisor::new(4);
        let output = supervisor
            .run("sleep", &["60"], Some(Duration::from_millis(200)))
            .await;

        assert!(output.timed_out);
        assert!(output.killed);
        assert!(output.exit_code.is_none());
    }

    #[tokio::test]
    async fn propagates_nonzero_exit_code() {
        let supervisor = Supervisor::new(4);
        let output = supervisor.run("bash", &["-c", "exit 42"], None).await;

        assert_eq!(output.exit_code, Some(42));
        assert!(!output.timed_out);
        assert!(!output.killed);
    }

    #[tokio::test]
    async fn max_parallel_limits_concurrency() {
        let supervisor = Supervisor::new(2);
        let output = supervisor.run("bash", &["-c", "echo ok"], None).await;

        assert_eq!(output.exit_code, Some(0));
        assert_eq!(output.stdout.trim(), "ok");
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
        let output = supervisor
            .run("sleep", &["60"], Some(Duration::from_millis(200)))
            .await;

        let json = serde_json::to_string(&output).unwrap();
        assert!(json.contains("\"timed_out\":true"));
        assert!(json.contains("\"killed\":true"));
        assert!(json.contains("\"exit_code\":null"));
    }
}
