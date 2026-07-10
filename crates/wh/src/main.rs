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
}

fn main() -> ExitCode {
    let cli = Cli::parse();

    match run(cli, &mut io::stdout()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(io::stderr(), "wh: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli, stdout: &mut impl Write) -> io::Result<()> {
    match cli.command {
        Some(cmd) => {
            let command_name = match cmd {
                Command::Status => "cli.status",
                Command::Jobs => "cli.jobs",
            };
            if cli.json {
                let response = wh_core::status::status_response(command_name, Vec::new());
                serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
                stdout.write_all(b"\n")?;
            } else {
                stdout.write_all(b"No watched jobs.\n")?;
            }
        }
        None => {
            if cli.json {
                serde_json::to_writer(
                    &mut *stdout,
                    &wh_core::contract::Response::bootstrap_success(),
                )
                .map_err(io::Error::other)?;
                stdout.write_all(b"\n")?;
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;
    use std::str;

    use super::{Cli, run};

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
        use super::Command;

        let cli = Cli {
            json: true,
            command: Some(Command::Status),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        assert_eq!(v["schema_version"], 1);
        assert_eq!(v["command"], "cli.status");
        assert!(v["ok"].as_bool().unwrap());
        assert!(v["error"].is_null());
        assert!(v["data"]["jobs"].as_array().unwrap().is_empty());
    }

    #[test]
    fn jobs_json_emits_v1_envelope_with_jobs_in_data() {
        use super::Command;

        let cli = Cli {
            json: true,
            command: Some(Command::Jobs),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        assert_eq!(v["schema_version"], 1);
        assert_eq!(v["command"], "cli.jobs");
        assert!(v["ok"].as_bool().unwrap());
        assert!(v["error"].is_null());
        assert!(v["data"]["jobs"].as_array().unwrap().is_empty());
    }

    #[test]
    fn status_without_json_prints_human_readable() {
        use super::Command;

        let cli = Cli {
            json: false,
            command: Some(Command::Status),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        assert_eq!(str::from_utf8(&stdout).unwrap(), "No watched jobs.\n");
    }

    #[test]
    fn jobs_without_json_prints_human_readable() {
        use super::Command;

        let cli = Cli {
            json: false,
            command: Some(Command::Jobs),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        assert_eq!(str::from_utf8(&stdout).unwrap(), "No watched jobs.\n");
    }
}
